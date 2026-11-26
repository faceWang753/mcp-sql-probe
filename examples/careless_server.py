"""A small SQLite MCP server with the gaps mcp-sql-probe looks for.

Each gap is one that real servers have shipped: a failure is caught and returned as
ordinary text (so isError stays false), the connection can write, several statements
run in one call, and nothing limits the size of a result.

    mcp-sql-probe --stdio "python examples/careless_server.py" --dialect sqlite \
        --suite errors,readonly --i-own-this-database
"""

import json
import sqlite3

from fastmcp import FastMCP


def build() -> FastMCP:
    mcp = FastMCP("careless-sqlite")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE transactions (txn_id INTEGER PRIMARY KEY, amount REAL)")
    conn.commit()

    @mcp.tool
    def query(query: str) -> str:
        """Run SQL and return the rows."""
        try:
            *before, last = [s for s in query.split(";") if s.strip()] or [""]
            for statement in before:
                conn.execute(statement)
            rows = conn.execute(last).fetchall()
            conn.commit()
            return json.dumps(rows)
        except Exception as exc:  # looks careful; hides the failure from the client
            return f"Error: {exc}"

    return mcp


if __name__ == "__main__":
    build().run()
