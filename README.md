# mcp-sql-probe

Checks for MCP servers that let an agent run SQL.

It connects to your server the way an agent would, sends a handful of statements through
the server's own query tool, and reports what came back. It answers three questions:

1. **When a query fails, can the client tell?** A failed tool call should come back with
   `isError: true`. A server that catches the exception and returns `"Error: ..."` as
   ordinary text looks correct in code review, and an agent reads the message as data.
2. **Does read-only mode hold?** The probe tries to create a table directly, after a
   first statement, after a `COMMIT`, and (on PostgreSQL) inside a `DO` block and after
   changing a session setting. It then checks whether the table exists.
3. **Is anything limited?** It asks for 50,000 rows and runs a slow query, and reports
   whether the server capped the result or stopped the query.

## Why

I read the query-tool error path of 12 SQL MCP servers in November 2026. Four of them
returned a failed query as a normal result. All four were Python servers that caught the
exception and returned text. The write-up is in
[a comment on SEP-2145](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2145).
This tool is the check I wished those projects had in CI.

## Use it

```bash
pip install git+https://github.com/faceWang753/mcp-sql-probe

# a server already running over HTTP
mcp-sql-probe --url http://127.0.0.1:8000/mcp

# or let the probe start it over stdio
mcp-sql-probe --stdio "uvx your-mcp-server --your-flags" --dialect postgres
```

The default suite, `errors`, sends only statements that cannot succeed and one
`SELECT` of a constant. It is safe to run against any database.

The other two suites need a database you own, because one tries to create tables and the
other runs heavy queries:

```bash
mcp-sql-probe --url http://127.0.0.1:8000/mcp \
  --suite errors,readonly,limits --i-own-this-database
```

The output has this shape:

```
tool=query  param=query  dialect=sqlite  suites=errors

PASS         baseline                   A valid query succeeds
             answered in 0.01 s
FAIL         errors.syntax              A statement with a syntax error is flagged
             the failure came back as a normal result (isError false), so a client cannot tell it from data: Error: near "SELEC": syntax error
```

The exit code is 1 if any check fails, 0 otherwise, and 2 if the probe could not start.

### In GitHub Actions

```yaml
- uses: faceWang753/mcp-sql-probe@v0.1.0
  with:
    stdio: uvx your-mcp-server --your-flags
    dialect: postgres
```

Results are also written to the job summary.

## The checks

| Suite | Check | Fails when |
|---|---|---|
| always | `baseline` | a plain `SELECT` does not return its value |
| `errors` | `errors.syntax` | a syntax error comes back with `isError` false |
| `errors` | `errors.missing_table` | a query on a missing table comes back with `isError` false |
| `errors` | `errors.unknown_tool` | never; it reports how the server answers a call to a tool that does not exist |
| `readonly` | `readonly.create` | `CREATE TABLE` creates the table |
| `readonly` | `readonly.stacked` | `SELECT 1; CREATE TABLE ...` creates the table |
| `readonly` | `readonly.commit_stacked` | `COMMIT; CREATE TABLE ...` creates the table |
| `readonly` | `readonly.do_block` | PostgreSQL only: a `DO` block creates the table |
| `readonly` | `readonly.session_setting` | PostgreSQL only: `SET default_transaction_read_only = off` lets the next call write |
| `limits` | `limits.rows` | all 50,000 rows come back in one result |
| `limits` | `limits.time` | a slow query runs to the end, or is still running when the probe gives up |

A failure reported as a JSON-RPC error instead of a tool result is a `WARN`, not a
`FAIL`: the client can still tell that the call failed.

When a check cannot decide, it says `INCONCLUSIVE` and why. For example, a server that
refuses recursive queries cannot be tested for a row cap this way.

## How it decides

The probe does not trust the server's wording. To learn whether a table was created, it
selects a marker string from that table and looks for the marker in the output. The
marker is built by concatenation inside the SQL (`'mcpprobe' || 'yes'`), so a server
that echoes the statement back in an error message does not produce a match.

If a write check fails, the probe drops the table it created and says so. If it cannot,
it names the table so that you can.

## Limits

- It finds the gaps it looks for. Passing every check does not show that a server is safe.
- The `readonly` suite only makes sense for a server that claims to be read-only.
- It has been run against the two example servers in this repository, which is what CI
  does. I have not yet run this version against third-party servers.
- `limits.time` may leave the slow query running in the database if the server does not
  cancel it.
- MySQL's default recursion limit (1,000) makes `limits.rows` inconclusive there unless
  you pass `--rows 1000` or lower.
- Dialects: PostgreSQL, MySQL, SQLite and DuckDB. Only SQLite is exercised in CI.

## Examples

[`examples/careful_server.py`](examples/careful_server.py) is a 50-line SQLite server
that passes every check, and
[`examples/careless_server.py`](examples/careless_server.py) is one that fails them.
Reading the two side by side is the quickest way to see what each check is about.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Built with AI assistance (Claude Code). MIT licence.
