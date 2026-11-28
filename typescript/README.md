# Sluice for Node (`mcp-sluice`)

A passthrough MCP server. It sits between an MCP client and exactly one
downstream MCP server, mounts the downstream tools, and proxies calls through.
Every eligible result is materialized into an in-memory DuckDB scratch database
before it is returned, so the agent gets a short preview plus a table handle
instead of the payload, and runs read-only SQL over it with Sluice's own
`query` tool.

This is the TypeScript version. A Python version with the same behavior is on
PyPI as [`mcp-sluice`](https://pypi.org/project/mcp-sluice/). Both follow one
[spec](https://github.com/miles-automation/sluice/tree/main/spec) and pass one
[conformance suite](https://github.com/miles-automation/sluice/tree/main/conformance).
See the [repository README](https://github.com/miles-automation/sluice#readme)
for how to choose between them.

## Requirements

- Node.js 24 or later.
- macOS on Apple silicon, or Linux on x64 or arm64. DuckDB ships as a native
  binding (`@duckdb/node-api`, pinned to `2.0.1-r.1`), about 65 MB on macOS and
  100 MB on Linux.

## Install

```sh
npm install -g mcp-sluice
sluice --help
```

Or run it without installing: `npx -y mcp-sluice --config /path/to/sluice.toml`.

## Configure

Sluice reads a TOML file naming the one downstream server to proxy. The same
file works for the Python version. Start from
[`sluice.example.toml`](https://github.com/miles-automation/sluice/blob/main/sluice.example.toml):

```toml
[servers.gh]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }
```

`${VAR}` expands from Sluice's own environment at load time. Config resolution
order is `--config`, then `$SLUICE_CONFIG`, then `./sluice.toml`.

Point your MCP client at the `sluice` command:

```json
{
  "mcpServers": {
    "sluice": {
      "command": "sluice",
      "args": ["--config", "/absolute/path/to/sluice.toml"]
    }
  }
}
```

Bounded pagination (`[pagination.<tool>]`) and resource limits (`[limits]`) are
described in the
[Python README](https://github.com/miles-automation/sluice/blob/main/python/README.md#bounded-pagination);
the keys and defaults are the same.

## Differences from the Python version

These are recorded as one-sided in the spec:

- Sluice forwards the MCP client's declared capabilities on downstream calls, so
  a downstream built on the v2 TypeScript SDK can ask for input through it.
- If the SDK cannot compile a tool's output schema (for example draft-04), the
  tool is called without output validation instead of failing.
- A malformed downstream response fails that call at once.
- Whole-number floats render as `1` in `query` output, not `1.0`.
- An escaped surrogate next to a raw unpaired surrogate in a payload is not JSON.

## Security

The agent can only read tables Sluice created in its own scope. File, network
and extension access are locked off in DuckDB, spilling to disk is disabled,
and the `query` tool admits a single read-only `SELECT` checked against an
allowlist over DuckDB's own parse tree. DuckDB does not count every string
intermediate against `max_memory`, so DuckDB runs in a child process
(`dist/store-worker.js`) capped by `store_max_memory` (default 2 GiB, greater
than `duckdb_max_memory`). The child checks its own resident memory every 20 ms
and Sluice checks it from outside (through `ps` on macOS, `/proc` on Linux, where
the child also runs under `RLIMIT_DATA`). A query that passes the cap fails with
a `store reset` error, the store restarts empty, and every earlier table and
handle fails loudly. Tool calls keep working throughout. Expect an overshoot of
up to about 150 MB.

Report problems at https://github.com/miles-automation/sluice/issues.

## License

Apache-2.0.
