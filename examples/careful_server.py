"""A small SQLite MCP server that does the things mcp-sql-probe checks for.

It is here as a test fixture and as a short example of each behaviour:
failures are raised (so the result has isError: true), the connection is read-only,
one statement per call, results are capped, and a slow query is interrupted.

    mcp-sql-probe --stdio "python examples/careful_server.py" --dialect sqlite
"""

import json
import sqlite3
import time

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

MAX_ROWS = 100
MAX_SECONDS = 1.5


def build() -> FastMCP:
    mcp = FastMCP("careful-sqlite")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE transactions (txn_id INTEGER PRIMARY KEY, amount REAL)")
    conn.executemany("INSERT INTO transactions (amount) VALUES (?)", [(i * 1.5,) for i in range(20)])
    conn.commit()
    conn.execute("PRAGMA query_only = ON")

    @mcp.tool
    def execute_sql(sql: str) -> str:
        """Run one read-only SQL statement. Returns at most 100 rows."""
        deadline = time.monotonic() + MAX_SECONDS
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            # sqlite3 refuses more than one statement in execute().
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise ToolError(f"query failed: {exc}") from exc
        finally:
            conn.set_progress_handler(None, 0)
        truncated = len(rows) > MAX_ROWS
        return json.dumps({"rows": rows[:MAX_ROWS], "truncated": truncated})

    return mcp


if __name__ == "__main__":
    build().run()
