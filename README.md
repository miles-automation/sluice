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

Runtime memory is bounded in two places: `max_concurrent_materializations`
admits only a configured number of complete interception pipelines at once, and
`max_session_bytes` bounds retained payload/table state. Oldest calls are
evicted deterministically when the retention budget is full; their envelope
metadata remains available, but their tables are no longer queryable. These are
logical bounds rather than process-RSS guarantees. After the whole-pipeline
bound landed, the default `max_payload_bytes` was calibrated down to 1 MiB;
`max_session_calls` independently bounds envelope
metadata rows and scope-view/catalog cardinality; a stale-table diagnostic cache
is bounded by the same call cap and may eventually fall back to the ordinary
unknown-table message.
