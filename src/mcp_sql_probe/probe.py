"""Checks for an MCP server that exposes a SQL query tool.

Every check sends SQL through the server's own tool and judges the result by what comes
back over MCP. Nothing here connects to the database directly.

A check never trusts the server's wording. Where it needs to know whether something
happened (a table was created, every row came back), it looks for a marker string that
can only appear in the output if the database produced it. Markers are built by
concatenation inside the SQL, so a server that echoes the statement in an error message
does not produce a false match.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fastmcp import Client

PASS, FAIL, WARN, INFO, INCONCLUSIVE, SKIP = "PASS", "FAIL", "WARN", "INFO", "INCONCLUSIVE", "SKIP"

DIALECTS = ("postgres", "mysql", "sqlite", "duckdb")
SUITES = ("errors", "readonly", "limits")

# Tool and parameter names seen across SQL MCP servers, most specific first.
TOOL_NAMES = ("execute_sql", "run_sql", "run_query", "execute_query", "read_query", "query", "sql")
PARAM_NAMES = ("sql", "query", "statement")


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str


@dataclass
class Outcome:
    """What one tool call came back as."""

    kind: str  # "ok" | "tool_error" | "protocol_error" | "timeout"
    text: str
    seconds: float

    @property
    def excerpt(self) -> str:
        return " ".join(self.text.split())[:160]


@dataclass
class Report:
    tool: str
    param: str
    dialect: str
    suites: list[str]
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class ProbeError(Exception):
    """The probe could not start (no SQL tool found, bad options)."""


def _text_of(result: Any) -> str:
    parts = [getattr(block, "text", None) or str(block) for block in (getattr(result, "content", None) or [])]
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        parts.append(json.dumps(structured, default=str))
    return " ".join(parts)


def _schema_of(tool: Any) -> dict:
    # fastmcp 4 names this input_schema; the camelCase name still works but warns.
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema or {}


def pick_tool(tools: list[Any], tool_name: str | None, param_name: str | None) -> tuple[str, str]:
    """Choose the SQL tool and its statement parameter, or raise ProbeError."""
    names = [t.name for t in tools]
    if tool_name:
        tool = next((t for t in tools if t.name == tool_name), None)
        if tool is None:
            raise ProbeError(f"tool {tool_name!r} not found; the server lists: {', '.join(names) or 'no tools'}")
    else:
        tool = next((t for wanted in TOOL_NAMES for t in tools if t.name == wanted), None)
        if tool is None:
            tool = next((t for t in tools if "sql" in t.name.lower() or "query" in t.name.lower()), None)
        if tool is None:
            raise ProbeError(f"no SQL tool found; pass --tool. The server lists: {', '.join(names) or 'no tools'}")

    props = list(_schema_of(tool).get("properties", {}))
    if param_name:
        if props and param_name not in props:
            raise ProbeError(f"tool {tool.name!r} has no parameter {param_name!r}; it has: {', '.join(props)}")
        return tool.name, param_name
    param = next((p for p in PARAM_NAMES if p in props), None)
    if param is None:
        if len(props) != 1:
            raise ProbeError(f"cannot tell which parameter of {tool.name!r} takes the SQL; pass --param. It has: {props}")
        param = props[0]
    return tool.name, param


class Probe:
    def __init__(
        self,
        client: Client,
        tool: str,
        param: str,
        dialect: str = "postgres",
        rows: int = 50_000,
        slow_seconds: float = 20.0,
    ) -> None:
        if dialect not in DIALECTS:
            raise ProbeError(f"unknown dialect {dialect!r}; choose one of {', '.join(DIALECTS)}")
        self.client = client
        self.tool = tool
        self.param = param
        self.dialect = dialect
        self.rows = rows
        self.slow_seconds = slow_seconds
        self.run_id = secrets.token_hex(4)
        self.checks: list[Check] = []

    # -- SQL helpers -------------------------------------------------------------------

    def concat(self, *parts: str) -> str:
        if self.dialect == "mysql":
            return f"CONCAT({', '.join(parts)})"
        return " || ".join(parts)

    def as_text(self, expr: str) -> str:
        return f"CAST({expr} AS CHAR)" if self.dialect == "mysql" else f"CAST({expr} AS VARCHAR)"

    # -- plumbing ----------------------------------------------------------------------

    async def call(self, sql: str, timeout: float = 60.0, tool: str | None = None) -> Outcome:
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.client.call_tool(tool or self.tool, {self.param: sql}, raise_on_error=False),
                timeout,
            )
            kind = "tool_error" if result.is_error else "ok"
            text = _text_of(result)
        except asyncio.TimeoutError:
            kind, text = "timeout", f"no answer after {timeout:.0f} s"
        except Exception as exc:  # noqa: BLE001 - any client-side error is a protocol-level failure
            kind, text = "protocol_error", f"{type(exc).__name__}: {exc}"
        return Outcome(kind, text, round(time.monotonic() - start, 2))

    def add(self, id: str, title: str, status: str, detail: str) -> None:
        self.checks.append(Check(id, title, status, detail))

    async def table_exists(self, table: str) -> bool:
        marker = self.concat("'mcpprobe'", "'yes'")
        out = await self.call(f"SELECT {marker} AS m FROM (SELECT count(*) AS c FROM {table}) t")
        return out.kind == "ok" and "mcpprobeyes" in out.text

    # -- suite: errors -----------------------------------------------------------------

    async def baseline(self) -> bool:
        marker = self.concat("'mcpprobe'", "'ok'")
        out = await self.call(f"SELECT {marker} AS m")
        if out.kind == "ok" and "mcpprobeok" in out.text:
            self.add("baseline", "A valid query succeeds", PASS, f"answered in {out.seconds} s")
            return True
        self.add(
            "baseline",
            "A valid query succeeds",
            FAIL,
            f"a plain SELECT did not return its value ({out.kind}): {out.excerpt}. "
            "The other checks were skipped because their results would mean nothing.",
        )
        return False

    async def _expect_flagged(self, id: str, title: str, sql: str) -> None:
        out = await self.call(sql)
        if out.kind == "tool_error":
            self.add(id, title, PASS, "result has isError: true")
        elif out.kind == "protocol_error":
            self.add(
                id,
                title,
                WARN,
                "reported as a protocol error, not a tool result. The MCP specification describes "
                f"tool execution failures as results with isError: true. Got: {out.excerpt}",
            )
        elif out.kind == "timeout":
            self.add(id, title, INCONCLUSIVE, out.text)
        else:
            self.add(
                id,
                title,
                FAIL,
                f"the failure came back as a normal result (isError false), so a client cannot tell it from data: {out.excerpt}",
            )

    async def suite_errors(self) -> None:
        await self._expect_flagged("errors.syntax", "A statement with a syntax error is flagged", "SELEC 1")
        await self._expect_flagged(
            "errors.missing_table",
            "A query on a missing table is flagged",
            f"SELECT * FROM _mcp_probe_missing_{self.run_id}",
        )
        out = await self.call("SELECT 1", tool=f"_mcp_probe_no_such_tool_{self.run_id}")
        how = {
            "tool_error": "a result with isError: true",
            "protocol_error": "a protocol error",
            "ok": "a normal result (isError false)",
            "timeout": "no answer",
        }[out.kind]
        self.add("errors.unknown_tool", "Calling a tool that does not exist", INFO, f"answered with {how}: {out.excerpt}")

    # -- suite: readonly ---------------------------------------------------------------

    async def _expect_no_write(self, id: str, title: str, table: str, statements: list[str]) -> None:
        last = None
        for sql in statements:
            last = await self.call(sql)
        if await self.table_exists(table):
            dropped = (await self.call(f"DROP TABLE {table}")).kind == "ok" and not await self.table_exists(table)
            cleanup = "The probe dropped it again." if dropped else f"The probe could not drop it; remove table {table} yourself."
            self.add(id, title, FAIL, f"the table was created. {cleanup}")
            return
        note = ""
        if last is not None and last.kind == "ok":
            note = " The server did not flag the refusal as an error."
        self.add(id, title, PASS, f"nothing was written.{note}")

    async def suite_readonly(self) -> None:
        def t(suffix: str) -> str:
            return f"_mcp_probe_{self.run_id}_{suffix}"

        await self._expect_no_write("readonly.create", "CREATE TABLE is refused", t("w"), [f"CREATE TABLE {t('w')} (i int)"])
        await self._expect_no_write(
            "readonly.stacked",
            "A write after a first statement is refused",
            t("s"),
            [f"SELECT 1; CREATE TABLE {t('s')} (i int)"],
        )
        await self._expect_no_write(
            "readonly.commit_stacked",
            "A write after COMMIT is refused",
            t("c"),
            [f"COMMIT; CREATE TABLE {t('c')} (i int)"],
        )
        if self.dialect == "postgres":
            await self._expect_no_write(
                "readonly.do_block",
                "A write inside a DO block is refused",
                t("d"),
                [f"DO $$ BEGIN CREATE TABLE {t('d')} (i int); END $$"],
            )
            await self._expect_no_write(
                "readonly.session_setting",
                "Turning read-only off for the session does not carry over",
                t("e"),
                ["SET default_transaction_read_only = off", f"CREATE TABLE {t('e')} (i int)"],
            )

    # -- suite: limits -----------------------------------------------------------------

    async def check_row_cap(self) -> None:
        id, title, n = "limits.rows", "A very large result is capped", self.rows
        label = self.concat("'mcpprobe_r'", self.as_text("n"), "'x'")
        sql = (
            f"WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < {n}) "
            f"SELECT {label} AS m FROM t"
        )
        out = await self.call(sql, timeout=120.0)
        if out.kind != "ok":
            self.add(id, title, INCONCLUSIVE, f"the {n}-row query did not run ({out.kind}): {out.excerpt}")
        elif f"mcpprobe_r{n}x" in out.text:
            self.add(id, title, FAIL, f"all {n} rows came back ({len(out.text):,} characters in one tool result)")
        elif "mcpprobe_r1x" in out.text:
            self.add(id, title, PASS, f"the result stopped before row {n} ({len(out.text):,} characters)")
        else:
            self.add(id, title, INCONCLUSIVE, f"the result held neither the first nor the last row: {out.excerpt}")

    def _slow_statements(self) -> list[str]:
        secs = int(self.slow_seconds)
        burn = "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 300000000) SELECT count(*) FROM t"
        if self.dialect == "postgres":
            return [f"SELECT pg_sleep({secs})", burn]
        if self.dialect == "mysql":
            return [f"SELECT SLEEP({secs})"]
        return [burn]

    async def check_timeout(self) -> None:
        id, title, limit = "limits.time", "A slow query is stopped", self.slow_seconds
        quick = min(2.0, limit / 4)
        last = None
        for sql in self._slow_statements():
            out = last = await self.call(sql, timeout=limit + 15)
            if out.kind in ("tool_error", "protocol_error") and out.seconds < quick:
                continue  # refused outright (for example by a function allowlist); try the next statement
            if out.kind == "timeout":
                self.add(id, title, FAIL, f"still running when the probe gave up after {out.seconds:.0f} s. It may still be running in the database.")
            elif out.kind == "ok" and out.seconds >= limit - 1:
                self.add(id, title, FAIL, f"ran to the end ({out.seconds} s) with no limit applied")
            elif out.kind == "ok":
                self.add(id, title, INCONCLUSIVE, f"the statement finished in {out.seconds} s, too fast to show a limit")
            else:
                self.add(id, title, PASS, f"stopped after {out.seconds} s: {out.excerpt}")
            return
        self.add(id, title, INCONCLUSIVE, f"the server refused every slow statement the probe has for {self.dialect}: {last.excerpt if last else ''}")

    async def suite_limits(self) -> None:
        await self.check_row_cap()
        await self.check_timeout()

    # -- entry -------------------------------------------------------------------------

    async def run(self, suites: list[str]) -> Report:
        unknown = [s for s in suites if s not in SUITES]
        if unknown:
            raise ProbeError(f"unknown suite {', '.join(unknown)}; choose from {', '.join(SUITES)}")
        report = Report(self.tool, self.param, self.dialect, suites, self.checks)
        if not await self.baseline():
            return report
        for suite in SUITES:  # fixed order: cheapest and safest first
            if suite in suites:
                await getattr(self, f"suite_{suite}")()
        return report


async def run_probe(
    target: Any,
    suites: list[str],
    dialect: str = "postgres",
    tool: str | None = None,
    param: str | None = None,
    rows: int = 50_000,
    slow_seconds: float = 20.0,
    auth: str | None = None,
) -> Report:
    """Connect to `target` (a URL, a transport, or a FastMCP instance) and run the suites."""
    kwargs = {"auth": auth} if auth else {}
    async with Client(target, **kwargs) as client:
        tool_name, param_name = pick_tool(await client.list_tools(), tool, param)
        probe = Probe(client, tool_name, param_name, dialect, rows, slow_seconds)
        return await probe.run(suites)
