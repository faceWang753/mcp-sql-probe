"""Command line for mcp-sql-probe.

Every option can also come from an environment variable (MCP_PROBE_URL, MCP_PROBE_STDIO,
MCP_PROBE_SUITE, MCP_PROBE_DIALECT, MCP_PROBE_TOOL, MCP_PROBE_PARAM,
MCP_PROBE_OWN_DATABASE). The GitHub Action uses those, so no input is ever pasted into a
shell command.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys

from .probe import DIALECTS, FAIL, SUITES, ProbeError, Report, run_probe

WRITES_OR_LOADS = ("readonly", "limits")


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp-sql-probe",
        description="Check how an MCP server with a SQL query tool reports errors, enforces read-only mode and limits results.",
    )
    where = p.add_mutually_exclusive_group()
    where.add_argument("--url", default=_env("MCP_PROBE_URL"), help="HTTP or SSE address of a running server")
    where.add_argument("--stdio", default=_env("MCP_PROBE_STDIO"), help='command that starts the server over stdio, for example "uvx my-server --flag"')
    p.add_argument("--suite", default=_env("MCP_PROBE_SUITE") or "errors", help=f"comma-separated list from: {', '.join(SUITES)} (default: errors)")
    p.add_argument("--dialect", default=_env("MCP_PROBE_DIALECT") or "postgres", choices=DIALECTS)
    p.add_argument("--tool", default=_env("MCP_PROBE_TOOL"), help="name of the SQL tool, if it is not found automatically")
    p.add_argument("--param", default=_env("MCP_PROBE_PARAM"), help="name of the tool parameter that takes the SQL")
    p.add_argument("--rows", type=int, default=50_000, help="rows requested by the row-cap check (default: 50000)")
    p.add_argument("--slow-seconds", type=float, default=20.0, help="how long the slow query tries to run (default: 20)")
    p.add_argument("--bearer-env", metavar="NAME", help="name of an environment variable holding a bearer token for --url")
    p.add_argument("--json", metavar="FILE", help="also write the report as JSON to FILE")
    p.add_argument(
        "--i-own-this-database",
        action="store_true",
        default=(_env("MCP_PROBE_OWN_DATABASE") or "").lower() in ("1", "true", "yes"),
        help="required for the readonly and limits suites, which try to create tables and run heavy queries",
    )
    return p


def render(report: Report) -> str:
    lines = [f"tool={report.tool}  param={report.param}  dialect={report.dialect}  suites={','.join(report.suites)}", ""]
    for c in report.checks:
        lines.append(f"{c.status:<12} {c.id:<26} {c.title}")
        lines.append(f"{'':<12} {c.detail}")
    counts: dict[str, int] = {}
    for c in report.checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    lines += ["", "  ".join(f"{k}: {v}" for k, v in counts.items())]
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    rows = ["### mcp-sql-probe", "", "| Result | Check | Detail |", "|---|---|---|"]
    for c in report.checks:
        detail = c.detail.replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {c.status} | {c.title} (`{c.id}`) | {detail} |")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suites = [s.strip() for s in args.suite.split(",") if s.strip()]

    if not args.url and not args.stdio:
        print("error: give --url or --stdio", file=sys.stderr)
        return 2
    risky = [s for s in suites if s in WRITES_OR_LOADS]
    if risky and not args.i_own_this_database:
        print(
            f"error: the {' and '.join(risky)} suite tries to create tables and runs heavy queries. "
            "Run it only against a database you own, and pass --i-own-this-database to confirm.",
            file=sys.stderr,
        )
        return 2

    auth = None
    if args.bearer_env:
        auth = os.environ.get(args.bearer_env)
        if not auth:
            print(f"error: environment variable {args.bearer_env} is empty", file=sys.stderr)
            return 2

    if args.url:
        target = args.url
    else:
        from fastmcp.client.transports import StdioTransport

        command, *rest = shlex.split(args.stdio, posix=os.name != "nt")
        # A stdio server only inherits a short allowlist of variables, so pass the full
        # environment: database settings usually arrive that way.
        target = StdioTransport(command=command, args=rest, env=dict(os.environ))

    try:
        report = asyncio.run(
            run_probe(target, suites, args.dialect, args.tool, args.param, args.rows, args.slow_seconds, auth)
        )
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(report.to_json())
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(render_markdown(report))
    return 1 if any(c.status == FAIL for c in report.checks) else 0


if __name__ == "__main__":
    sys.exit(main())
