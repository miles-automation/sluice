# Sluice

A passthrough MCP server. It sits between an MCP client and a downstream MCP
server, mounts the downstream tools, and proxies calls through. Every result is
materialized into a DuckDB scratch database before being returned, so the agent
gets a short preview plus a table handle instead of the payload, and runs SQL
over it with Sluice's own `query` tool.

Two things this buys: aggregation becomes exact rather than a model reading rows
out of a payload, and a large payload never enters the context window, so it is
never re-sent on later turns of the agent loop.

Status: pre-implementation. `intent/`, `spec/`, and `plan/` hold the design;
`plan/001-notes-m0.md` holds the measured evidence behind it. Read the spec
before changing behavior.
