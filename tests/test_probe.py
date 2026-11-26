"""The probe must tell a careful server from a careless one.

Both servers run in memory through fastmcp's in-process transport, so these tests need
no network and no database server.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

import careful_server  # noqa: E402
import careless_server  # noqa: E402

from mcp_sql_probe import ProbeError, run_probe  # noqa: E402
from mcp_sql_probe.cli import main, render, render_markdown  # noqa: E402
from mcp_sql_probe.probe import FAIL, INFO, PASS, pick_tool  # noqa: E402


def probe(server, suites, **kwargs):
    return asyncio.run(run_probe(server.build(), suites, dialect="sqlite", **kwargs))


def status(report):
    return {c.id: c.status for c in report.checks}


def test_careful_server_passes_error_checks():
    got = status(probe(careful_server, ["errors"]))
    assert got["baseline"] == PASS
    assert got["errors.syntax"] == PASS
    assert got["errors.missing_table"] == PASS
    assert got["errors.unknown_tool"] == INFO


def test_careless_server_fails_error_checks():
    report = probe(careless_server, ["errors"])
    got = status(report)
    assert got["baseline"] == PASS
    assert got["errors.syntax"] == FAIL
    assert got["errors.missing_table"] == FAIL
    assert report.failed


def test_careful_server_refuses_writes():
    got = status(probe(careful_server, ["readonly"]))
    assert got["readonly.create"] == PASS
    assert got["readonly.stacked"] == PASS
    assert got["readonly.commit_stacked"] == PASS
    # PostgreSQL-only checks are not run for sqlite.
    assert "readonly.do_block" not in got


def test_careless_server_writes_and_probe_cleans_up():
    report = probe(careless_server, ["readonly"])
    got = status(report)
    assert got["readonly.create"] == FAIL
    assert got["readonly.stacked"] == FAIL
    details = {c.id: c.detail for c in report.checks}
    assert "dropped it again" in details["readonly.create"]


def test_row_cap():
    careful = status(probe(careful_server, ["limits"], rows=2000, slow_seconds=4))
    assert careful["limits.rows"] == PASS
    assert careful["limits.time"] == PASS


def test_no_row_cap_is_reported():
    # Only the row check: the careless server has no time limit, and its slow query
    # would hold the test for as long as SQLite takes to count to 300 million.
    async def run():
        from fastmcp import Client

        from mcp_sql_probe.probe import Probe

        async with Client(careless_server.build()) as client:
            p = Probe(client, "query", "query", dialect="sqlite", rows=2000)
            await p.check_row_cap()
            return p.checks

    checks = asyncio.run(run())
    assert checks[0].status == FAIL
    assert "all 2000 rows" in checks[0].detail


def test_tool_and_param_are_found_automatically():
    assert probe(careful_server, ["errors"]).tool == "execute_sql"
    report = probe(careless_server, ["errors"])
    assert (report.tool, report.param) == ("query", "query")


def test_unknown_tool_name_is_a_clear_error():
    with pytest.raises(ProbeError, match="not found"):
        probe(careful_server, ["errors"], tool="nope")


def test_unknown_suite_is_a_clear_error():
    with pytest.raises(ProbeError, match="unknown suite"):
        probe(careful_server, ["everything"])


def test_pick_tool_needs_help_when_nothing_looks_like_sql():
    class Tool:
        name = "weather"
        input_schema = {"properties": {"city": {}}}

    with pytest.raises(ProbeError, match="no SQL tool found"):
        pick_tool([Tool()], None, None)


def test_reports_render():
    report = probe(careless_server, ["errors"])
    assert "errors.syntax" in render(report)
    assert "| FAIL |" in render_markdown(report)
    assert '"status": "FAIL"' in report.to_json()


def test_cli_refuses_risky_suites_without_confirmation(capsys):
    code = main(["--url", "http://127.0.0.1:1/mcp", "--suite", "readonly"])
    assert code == 2
    assert "--i-own-this-database" in capsys.readouterr().err


def test_cli_needs_a_target(capsys, monkeypatch):
    monkeypatch.delenv("MCP_PROBE_URL", raising=False)
    monkeypatch.delenv("MCP_PROBE_STDIO", raising=False)
    assert main([]) == 2
    assert "--url or --stdio" in capsys.readouterr().err
