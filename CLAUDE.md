# CLAUDE.md - Sluice

Passthrough MCP server. Proxies **exactly one** downstream MCP server in v0
(namespacing is built for fan-out, deferred), materializes every tool result into
a DuckDB scratch DB, and returns a preview plus a table handle instead of the
payload. Exposes one tool of its own, `query`, for read-only SQL over those
tables.

Read `spec/001-scratch-db.md` before changing behavior; `intent/` records why the
non-goals are non-goals; `plan/001-notes-m0.md` is the measured evidence behind
the rules below.

## Commands

```bash
uv sync                       # install
uv run sluice --config sluice.toml   # run as an MCP stdio server
uv run pytest                 # full suite
uv run pytest -m "not slow"   # skip the Hypothesis property test
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run python -m demo.median  # the eval; not part of CI, calls a model
```

## Python version rules

- `requires-python = ">=3.14"`. **Do not write compatibility shims for older
  versions.** No `sys.version_info` branches, no backport imports.
- PEP 604 unions: `X | None`, never `typing.Optional[X]`.
- PEP 585 builtin generics: `list[str]`, `dict[str, int]`, never `typing.List`.
- `type` statements for aliases, never `TypeAlias`.
- Type hints on every function signature. `mypy` is not optional.
- Python 3.15 releases 2026-10-01. Moving the floor to 3.15 is an **open
  question, not a decision**. The gating factor is DuckDB cp315 wheel
  availability, since DuckDB is a compiled extension and a source build is not an
  acceptable install path.

## Conventions

- Idiomatic modern Python of the FastAPI and Postgres kind. Dataclasses for value
  objects, `pathlib`, structured logging **to stderr** (stdout is the MCP
  transport and must never be written to).
- `shape.py`, `infer.py`, `naming.py` are pure: no DuckDB, no MCP, no IO. They
  hold the logic most likely to be wrong and are cheapest to test. Keep them pure.
- DuckDB calls are blocking: dispatch through `asyncio.to_thread`, serialize
  writes behind an `asyncio.Lock`, one connection per in-flight query.
- pytest, function-style tests, Hypothesis for the correctness property.

## Architecture

```
client --stdio--> server.py  tools/list = downstream union + query
                  proxy.py   downstream session, paginated list, round-trip relay
                  shape.py   extract rows -> depth-1 projection        (pure)
                  infer.py   column types + the `exact` flag           (pure)
                  naming.py  injective names, quoting, collisions      (pure)
                  scope.py   scope ids; stale handles fail loudly
                  store.py   envelope row + typed tables
                  handle.py  preview + tables + columns -> the agent
                  query.py   three-layer gate, timeout, caps
```

Data model: one `sluice_calls` envelope row per call. Plus one typed table per
candidate array, never appended to, named
`<server>__<tool>__<hash>__<scope>__<seq>`. There is no `__latest` view: with
scope minted per call it would name one table, and reaching a table whose name
you lost is discovery, which isolation blocks. Tables and the envelope row are
written in one transaction.

Pinned: MCP protocol `2026-07-28`, `mcp` 2.1.1, DuckDB 1.5.5. Every normative
claim in the spec is against those; do not generalize across revisions.

## Rules that are load-bearing

Each of these was a bug before it was a rule. Spec section in parentheses.

- **Sluice never turns a working tool call into a failed one** (§8). Failures
  degrade to an envelope-only handle. Two boundaries: OOM, and a connection-wide
  interrupt.
- **The handle rides in `content`** (§4.1); `structuredContent` mirrors it.
  **On the way in, `structuredContent` wins** (§5.1): a tool may put data there
  and prose in `content`, and flattening the prose discards the data.
- **Exactness is domain-bounded** (§5.6). `median` equals Python exactly inside
  the domain, not outside; `avg` never does. Never state the claim without it.
- **Materialization is file-free** (§5.4). The lockdown is database-global and
  blocks DuckDB's own readers. Do not "fix" a load failure by relaxing it.
- **Sluice owns type inference** (§5.5), because of the above. Never infer
  `TIMESTAMP` from a string; mixed scalars become `VARCHAR`, never `JSON`
  (`median()` on `JSON` returns a lexicographic answer).
- **Read-only means three layers** (§6.1): statement gate, engine lockdown,
  catalog denylist. `SELECT * FROM read_csv('/etc/passwd')` is a SELECT.
- **No table discovery** (§12). Enumeration is what isolation blocks. Do not add
  a `sluice_schema` view back.
- **Sluice never answers an elicitation** (§11). Round trips are relayed
  untouched; `request_state` is opaque.
- **Clone the whole downstream tool object** (FR-3), mutating only name,
  description, and `outputSchema`. Rebuilding it field by field drops
  `annotations.destructiveHint`.
- **Every truncation is reported** (§6.3). Silent truncation is a correctness bug
  in a tool that sells determinism.

## Out of scope for v0

No cross-session persistence, cross-server joins, entity resolution, auth,
policy, redaction, audit layer, hosted service, UI, or table discovery. Recorded
as choices in `intent/` §Non-goals, not as backlog.
