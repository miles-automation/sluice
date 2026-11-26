# Sluice

A passthrough MCP server. It sits between an MCP client and exactly one
downstream MCP server, materializes every eligible tool result into an
in-memory DuckDB scratch database, and returns a short preview plus a table
handle instead of the payload. The agent runs read-only SQL over those tables
with Sluice's own `query` tool.

## Layout

| Path | Contents |
| --- | --- |
| [`python/`](python/) | The Python version, published on PyPI as [`mcp-sluice`](https://pypi.org/project/mcp-sluice/). Install, usage and configuration are in [`python/README.md`](python/README.md). |
| [`spec/`](spec/) | The normative behavior, shared by every version. |
| [`intent/`](intent/) | Why the non-goals are non-goals. |
| [`plan/`](plan/), [`review/`](review/) | Measured evidence and review records. |
| [`sluice.example.toml`](sluice.example.toml) | Example configuration. |

A TypeScript version for npm is in progress. It will live in `typescript/` and
follow the same spec.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
