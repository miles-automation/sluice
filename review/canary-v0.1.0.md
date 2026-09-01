# v0.1.0 real-downstream canary

Run on 2026-09-01 UTC from commit
`92fef107e2399120494258ad9b0e7e5196939fd9` on macOS arm64 with CPython
3.14.2, DuckDB 1.5.5, MCP Python SDK 2.1.1, Node 25.3.0, and the
`@modelcontextprotocol/server-github` 2025.4.8 stdio server.

## Configuration and credentials

Sluice launched one real downstream server through `npx` using the documented
stdio configuration shape. The GitHub token was resolved at runtime from Spark
Swarm into an environment variable and passed to the child process. The config
contained only an environment placeholder; no token value was printed, written
to the repository, or included in this report.

The npm package emits a deprecation warning and negotiated downstream MCP
protocol `2024-11-05`. This canary is compatibility evidence for Sluice, not a
recommendation to deploy that deprecated server. Sluice correctly exposed MCP
`2026-07-28` upstream and disabled downstream interactive round trips that the
legacy session cannot support.

## Observed checks

1. Sluice started through its installed `sluice --config` console entry point.
2. The downstream catalog mounted 26 real GitHub tools; Sluice exposed those 26
   plus its own `query` tool.
3. A live authenticated `search_repositories` call for
   `sluice in:name user:miles-automation` returned one result.
4. Sluice replaced the JSON result with a bounded handle containing a typed
   table, scope-filtered envelope view, and call id.
5. `SELECT count(*)` through Sluice's `query` tool returned the handle's exact
   row count (`1`).
6. Querying the scope-filtered envelope by call id returned the original
   `search_repositories` tool metadata.
7. `SELECT * FROM sluice_calls` was rejected as an unknown table, confirming the
   physical cross-scope envelope stayed outside the query allowlist.

Summary emitted by the canary harness:

```text
canary_ok upstream_protocol=2026-07-28 mounted_tools=26 rows=1 handle=yes query=yes envelope=yes isolation=yes
```

## Release evidence

- Merged-main CI for the canary commit, including 391 tests and clean-wheel
  smoke: <https://github.com/miles-automation/sluice/actions/runs/33462441530>
- Built version: `0.1.0`; wheel includes `py.typed` and a working `sluice` CLI.

Apache-2.0 and the `mcp-sluice` distribution name were approved for v0.1.0 and
are recorded in `LICENSE` and `pyproject.toml`. The remaining release operations
are making the repository public, configuring the PyPI Trusted Publisher, and
tagging and publishing `v0.1.0`.
