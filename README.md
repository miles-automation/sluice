# Sluice

A passthrough MCP server. It sits between an MCP client and exactly one
downstream MCP server, materializes every eligible tool result into an
in-memory DuckDB scratch database, and returns a short preview plus a table
handle instead of the payload. The agent runs read-only SQL over those tables
with Sluice's own `query` tool.

## Two versions

| | Python | TypeScript |
| --- | --- | --- |
| Package | PyPI [`mcp-sluice`](https://pypi.org/project/mcp-sluice/) | npm [`mcp-sluice`](https://www.npmjs.com/package/mcp-sluice) |
| Install | `pip install mcp-sluice` | `npm install -g mcp-sluice` |
| Runtime | Python 3.14+, `duckdb>=1.5.5` (CI runs 1.5.5 and current) | Node 24+, `@duckdb/node-api` pinned to `2.0.1-r.1` |
| MCP SDK | `mcp` (Python) | `@modelcontextprotocol/client` and `/server` 2.4.0 |
| Docs | [`python/README.md`](python/README.md) | [`typescript/README.md`](typescript/README.md) |

Both run as the `sluice` command and read the same `sluice.toml`.

**How to choose.** Use the one that matches the toolchain you already run MCP
servers with: `pip`/`uv` users take the Python version, `npx`/Node users the
TypeScript one. The TypeScript version keeps integers beyond 2^53 and the
difference between `1` and `1.0` exact on the way in, and can relay input
requests to downstreams built on the v2 TypeScript SDK; the Python version
supports a wider DuckDB range. Slopticus uses the TypeScript version.

**Parity rule.** [`spec/`](spec/) is normative for both. The language-neutral
suite in [`conformance/`](conformance/) runs in CI against both versions. A
behavior change lands in both with a fixture, or is recorded as one-sided in the
spec and the fixture. The current one-sided differences are listed in
[`typescript/README.md`](typescript/README.md#differences-from-the-python-version).

## Measured context

One real multi-page task: the ten employers with the most open "engineer"
listings in a local snapshot of the BullshitOrFit jobs database (3,661 rows,
19 pages of 200), asked of Claude through Claude Code with the same prompt and
tool scoping in every condition. All runs got the exact answer. Full method and
raw transcripts: `research/sluice-bof-comparison/RESULTS.md` in the workspace.

| Condition | Runs | Tool calls | Total context tokens | Peak context |
| --- | --- | --- | --- | --- |
| Direct MCP only | 3 | 84 (mean) | 35.9M (mean) | 957K |
| Direct MCP plus Bash/jq over spilled files | 3 | 43 | 2.96M | 106K |
| Python Sluice, page by page | 3 | 22 | 0.79M | 61K |
| Python Sluice, bounded pagination | 3 | 4 | 91K | 20K |
| TypeScript Sluice, bounded pagination | 1 | 4 | 51K | 15K |

The first four rows used `claude-sonnet-5` (2026-09-13). The TypeScript row was
run in late November 2026, when the same alias resolved to `claude-sonnet-5-5`, so it is
not a like-for-like comparison with the Python rows. Against MCP plus Bash/jq,
the fairest baseline for an agent with a shell, Sluice with bounded pagination
used about 30 times less context on this task. One task is evidence, not a
benchmark.

## Layout

| Path | Contents |
| --- | --- |
| [`python/`](python/) | The Python version. |
| [`typescript/`](typescript/) | The TypeScript version. |
| [`spec/`](spec/) | The normative behavior, shared by both. |
| [`conformance/`](conformance/) | JSON fixtures both versions run in CI. |
| [`intent/`](intent/) | Why the non-goals are non-goals. |
| [`plan/`](plan/), [`review/`](review/) | Measured evidence and review records. |
| [`sluice.example.toml`](sluice.example.toml) | Example configuration. |

## Releases

Each version is released independently from a tag: `vX.Y.Z` publishes the
Python package to PyPI, `typescript-vX.Y.Z` publishes the TypeScript package to
npm.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
