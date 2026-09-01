# Sluice

A passthrough MCP server. It sits between an MCP client and **exactly one**
downstream MCP server, mounts the downstream tools, and proxies calls through.
Every eligible result is materialized into an in-memory DuckDB scratch database
before being returned, so the agent gets a short preview plus a table handle
instead of the payload, and runs SQL over it with Sluice's own `query` tool.

Two things this buys: aggregation becomes exact rather than a model reading
rows out of a payload, and an eligible payload is replaced by a bounded handle,
so it is not re-sent on later turns of the agent loop. Error, binary, and
over-ceiling results deliberately pass through unchanged.

## Status

The v0.1.0 release candidate is complete and is intended to become the first
public release. Proxying, envelope/handle recording, flattening and type
inference, the read-only `query` gate, aggregate-correctness properties,
bounded runtime retention, and final adversarial review are implemented and
covered by the test suite. The reproducible live-model demo recorded one
baseline miss (71.5) and one Sluice-backed exact answer (72.5); that sampled
run is evidence, not a deterministic guarantee.

CI runs the full test, lint, formatting, strict type-check, and package-build
gates. The end-to-end canary against a real configured GitHub MCP server passed;
its evidence is in `review/canary-v0.1.0.md`. The project is distributed as
`mcp-sluice`; the Python import package and console command remain `sluice`.

Read `spec/001-scratch-db.md` before changing behavior; `intent/` records why
the non-goals are non-goals; `plan/001-notes-m0.md` holds the measured
third-party behavior the design rests on.

**Behavioral baseline verified against:** MCP protocol `2026-07-28`, SDK `mcp`
2.1.1, DuckDB 1.5.5, CPython 3.14. Behavior differs across revisions of any of
these; the spec does not generalize past what was measured.

## Installation

Requires Python 3.14+ (`requires-python = ">=3.14"` — no compatibility shims
for older interpreters) and a platform DuckDB 1.5.5 ships a wheel for.

After v0.1.0 is published, install the distribution from PyPI:

```bash
pip install mcp-sluice
sluice --config sluice.toml
```

Or, from a checkout with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run sluice --config sluice.toml
```

You can also build and install a wheel into any environment:

```bash
uv build                          # writes dist/mcp_sluice-0.1.0-py3-none-any.whl
pip install dist/mcp_sluice-0.1.0-py3-none-any.whl
sluice --config sluice.toml
```

Dependency metadata deliberately carries lower bounds, not exact pins, and no
lock file is committed. Both install paths therefore resolve current releases of
`mcp` and DuckDB, which may be newer than the verified baseline. CI upgrades to
current dependencies and runs the engine-contract tripwires, but those tests are
not part of the wheel. For a deployment, build from a checkout at a green commit
and run its test suite against the resolved environment before rollout.

## Configuring an MCP client

Copy `sluice.example.toml` to `sluice.toml` and point it at the one downstream
server you want to proxy:

```toml
[servers.gh]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }
```

`${VAR}` expands from Sluice's own environment at load time, so secrets stay
out of the file. v0 refuses to start if `[servers]` defines more than one
entry — the table shape admits several because tool namespacing is built for
fan-out, but fan-out itself is not implemented (FR-7).

Then point your MCP client at the installed console script, e.g. for a client
that spawns stdio servers from a JSON config:

```json
{
  "mcpServers": {
    "sluice": {
      "command": "uv",
      "args": [
        "run", "--directory", "/absolute/path/to/sluice",
        "sluice", "--config", "/absolute/path/to/sluice.toml"
      ]
    }
  }
}
```

If you installed a wheel instead of using `uv run`, use the `sluice` console
script directly:

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

Config resolution order is `--config`, then `$SLUICE_CONFIG`, then
`./sluice.toml` in the current working directory.

### Resource bounds

The default `max_payload_bytes` is 1 MiB and counts both structured and text
channels together. An eligible result over that ceiling is passed through
unchanged with a size note; Sluice does not parse or retain a second copy.
`max_concurrent_materializations = 2` admits at most two complete interception
pipelines at once. `max_session_bytes = 256 MiB` bounds logical retained payload
and table representations, while `max_session_calls = 1000` separately bounds
envelope rows and scope views. Oldest calls are evicted first and stale handles
fail loudly.

These are operational and logical bounds, not a process-RSS guarantee.
`duckdb_max_memory = "1GB"` limits DuckDB's engine allocations, not the whole
process, and the MCP SDK may decode structured content before Sluice can measure
it. The [memory benchmark](benchmarks/results/memory-2026-08-30.md) records the
four-shape, dual-channel concurrency calibration and long-session evidence.

## Architecture

```
client --stdio--> server.py  tools/list = downstream union + query
                  proxy.py   downstream session, paginated list, round-trip relay
                  gate.py    the query tool's three-layer read-only gate
                  shape.py   extract rows -> depth-1 projection        (pure)
                  infer.py   column types + the `exact` flag           (pure)
                  naming.py  injective names, quoting, collisions      (pure)
                  scope.py   scope ids; stale handles fail loudly
                  store.py   envelope row + typed tables
                  handle.py  preview + tables + columns -> the agent
                  query.py   per-query connection, timeout, result shaping
```

One `sluice_calls` envelope row is written per proxied call, plus one typed
table per candidate array extracted from the result, named
`<server>__<tool>__<hash>__<scope>__<seq>`. Tables are never appended to.
There is no discovery mechanism and no `__latest` view — the agent reaches a
table only through the name in its own handle.

## Usage

Sluice's `tools/list` returns every downstream tool, renamed to
`<slug(server)>__<slug(tool)>__<hash>`, plus Sluice's own `query`. Calling a
mounted tool forwards to the real tool unchanged and returns a handle instead
of the payload:

```
sluice: result recorded.  channel=structured  scope=1f3a9c2e7b6d4a815c9f02e3b7a41d6c
table: gh__list_issues__3f9a1c__1f3a9c2e7b6d4a815c9f02e3b7a41d6c__0001   rows=412   from=$.items
columns: id BIGINT, number BIGINT, title VARCHAR, state VARCHAR,
         created_at VARCHAR, labels JSON*, user JSON*
         (* JSON: use json_extract and cast before arithmetic)
envelope: sluice_calls__1f3a9c2e7b6d4a815c9f02e3b7a41d6c WHERE call_id = '0c3f8e1a-...'
preview (first 3 of 412 rows, 1.9 KB of 91.4 KB):
  {"id": 1841, ...}
Run SQL over this with the `query` tool.
```

Run SQL over the materialized table with `query`:

```
query(sql="SELECT median(score) FROM \"gh__list_issues__3f9a1c__1f3a9c2e7b6d4a815c9f02e3b7a41d6c__0001\"")
```

`query` accepts exactly one read-only `SELECT` (or `WITH ... SELECT`), enforces
a wall-clock timeout, a configured row ceiling (100 by default), and
a byte cap on the rendered markdown table, and states explicitly whenever any
of those truncated the result — never a silent truncation.

To recover a full payload rather than aggregating it, select it back out of
the envelope view named in the handle:

```sql
SELECT result_structured FROM sluice_calls__1f3a9c2e7b6d4a815c9f02e3b7a41d6c WHERE call_id = '0c3f8e1a-...'
```

`max_cell_bytes` (512 by default) truncates long cells well before the query
byte cap, so recovering a large payload means chunking with `substr`, which
counts characters and is therefore safe against multi-byte UTF-8:

```sql
SELECT substr(result_text, 1, 4000) FROM sluice_calls__1f3a9c2e7b6d4a815c9f02e3b7a41d6c WHERE call_id = '0c3f8e1a-...'
```

## Exactness guarantees

Column types come from Sluice's own inference (`infer.py`), not DuckDB's JSON
inference — the query gate's lockdown blocks DuckDB's file readers, which
forecloses `read_json`. Each column in a handle is marked `exact: true` or
`exact: false`.

**Exact, inside the domain** (verified on DuckDB 1.5.5): `count(*)`,
`count(col)`, `min`, `max`, `count(DISTINCT)`, `GROUP BY` counts, integer
`sum`, and `median` on integer columns whose values remain within ±2^53.

**Bounded regression evidence, not a universal guarantee**: `avg` and float
`sum` are tested on a deliberately safe dyadic domain at a relative tolerance
of `1e-9` and an absolute tolerance of `1e-12`. Every `DOUBLE` column is marked
`exact: false`: cancellation can defeat both tolerances even at moderate
magnitudes, and a single column flag cannot encode per-aggregate error.

**Never claimed**: any column marked `exact: false` — every `DOUBLE` column,
integer columns with values beyond ±2^53, mixed scalar types (kept `VARCHAR`,
never `JSON`, because `median()` on `JSON` returns a lexicographic answer),
integers past `int128`, and any column containing a non-finite float.
ISO-8601-shaped strings are deliberately kept `VARCHAR` rather than inferred
as `TIMESTAMP`, since DuckDB's inference drops the timezone silently.

Missing keys and JSON `null` both normalize to SQL `NULL` in a flat table —
this is lossy and deliberate, and it is the reference the exactness claim is
defined against, not raw JSON.

See `spec/001-scratch-db.md` §5.5–5.6 for the full rules and measured
counterexamples outside the domain.

## Reproducible model demo

Run `uv run python -m demo.median --model haiku --dry-run` to inspect the
mechanically computed expected answer and generated configs without spending a
model call. Run `uv run python -m demo.median --model haiku` to reproduce the
committed run's requested model; the harness default is currently `sonnet`.
The committed sampled report is under `demo/transcripts/20260831T040623Z/`;
see `demo/README.md` for prerequisites, transcript handling, and the explicit
non-determinism caveat. The demo is deliberately outside CI and is not included
in the wheel, so run it from a source checkout.

## Security and isolation boundaries

- **Read-only means three layers**, not a `SELECT` prefix check: a statement
  gate (`extract_statements`, exactly one `SELECT`), an engine-wide lockdown
  (`enable_external_access = false` and friends, applied once and locked at
  session start), and an object allowlist over the parsed AST that only admits
  tables and views Sluice itself created. The statement gate alone does **not**
  stop `SHOW`, `DESCRIBE`, `SUMMARIZE`, or `PRAGMA` — DuckDB types all of them
  as `SELECT`. Only the allowlist stops them.
- **No table discovery.** `sluice_calls` (the physical envelope, listing every
  scope's tables) is never queryable. Each scope gets a filtered view,
  `sluice_calls__<scope>`, and that view — plus the table names in the agent's
  own handles — is the entire addressing model. There is no catalog to
  enumerate.
- **Scope isolation is capability-based, not enforced**, in v0. Table names
  carry a 128-bit scope tag, so a stale handle from a previous process cannot
  resolve to a live table holding different data — that part is unconditional.
  But without a client-supplied conversation id, isolation between two live
  conversations in the same process rests on unguessable table names plus the
  query allowlist, not on a hard boundary like a per-scope database. An agent
  that retains a handle across a conversation boundary can still reach that
  table. Enforced isolation (a conversation id from the client, or one DuckDB
  database per scope) is deferred to v1.
- **No cross-session persistence.** The database is in-memory and dies with
  the process.
- **Materialization never happens via disk.** The lockdown that makes `query`
  read-only is database-global, so it also blocks Sluice's own writer from
  `read_json` on a temp file. Tables are built with explicit `CREATE TABLE` +
  `executemany`, never a file-based loader.
- **Sluice never answers an elicitation or sampling request on the client's
  behalf.** Multi-round-trip calls are relayed end to end; `request_state` is
  forwarded opaque and untouched.
- **A tool call that already succeeded is never turned into a failure** by
  materialization. A parse or insert failure degrades to an envelope-only
  handle; only a process-ending OOM or a connection-scoped `query` interrupt
  are outside that guarantee, and both are named rather than implied.

## v0 limitations

Recorded as decisions in `intent/001-scratch-db.md` §Non-goals, not as an
incomplete backlog:

- **One downstream server per process.** Sluice refuses to start with more
  than one `[servers.*]` entry configured.
- **No cross-server joins or entity resolution.**
- **No auth, policy enforcement, redaction, or audit layer.**
- **No hosted service, no UI.** Local stdio process only.
- **Static tool catalog.** `tools/list` is captured once at startup
  (`listChanged: false`); a downstream server that adds tools at runtime needs
  a Sluice restart to be seen.
- **Progress notifications are not forwarded.** Cancellation is.
- **A tool that pages its own results** produces one table per page; the
  agent has to `UNION` them by hand. There is no per-tool `__latest` view.
- **JSON columns are an aggregation trap.** `sum()`/`avg()` raise on them
  (loud), but `median()` on a `JSON` column returns a lexicographic result
  rather than erroring (quiet) if the underlying values happen to look
  sortable as strings.
- Open questions not yet decided: whether the Python floor moves to 3.15
  (gated on a DuckDB cp315 wheel), and whether `_extra`/`JSON` columns need
  more ergonomics once used in practice. See `spec/001-scratch-db.md` §14.

## Troubleshooting

- **`sluice: no config file at ...`** — pass `--config path/to/sluice.toml`,
  set `$SLUICE_CONFIG`, or run from a directory containing `sluice.toml`.
- **`sluice: [limits].<name> must be ...`** — a limit in `[limits]` failed
  validation (wrong type or out of range). The message names the offending
  key; see `sluice.example.toml` for valid values.
- **`sluice: could not start downstream: ...`** — Sluice failed to connect to
  the configured downstream server and refuses to start in a degraded state
  (FR-6). Check that `command`/`args` resolve on `$PATH` and that any `${VAR}`
  referenced in `env` is actually set.
- **A query is rejected** — the error names the specific reason: not a single
  `SELECT`, references a table Sluice did not create, uses a schema-qualified
  name, or uses `SHOW`/`DESCRIBE`. There is no way to list what tables exist;
  the handles already in your conversation are the index.
- **The client reports the server crashed with no message** — Sluice logs
  diagnostics to stderr only, never stdout, because stdout is the MCP
  transport. Check your client's captured stderr for the process.
- **A large tool result comes back unmodified with a size note instead of a
  handle** — it exceeded `max_payload_bytes` (1 MiB default) and was passed
  through without being parsed or stored, by design (spec §5.1 step 2, §8).

## License

Sluice is licensed under the [Apache License 2.0](LICENSE).
