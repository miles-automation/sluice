# CLAUDE.md - Sluice

Passthrough MCP server. Proxies one or more downstream MCP servers, materializes
every tool result into a session-scoped DuckDB, and returns a preview plus a
table handle instead of the payload. Exposes one tool of its own, `query`, for
read-only SQL over those tables.

Read `spec/001-scratch-db.md` before changing behavior. `intent/001-scratch-db.md`
records why the non-goals are non-goals.

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

- Idiomatic modern Python of the FastAPI and Postgres kind. Dataclasses for
  value objects, `pathlib`, structured logging to stderr (stdout is the MCP
  transport and must never be written to directly).
- `shape.py` and `naming.py` are pure: no DuckDB imports, no MCP imports, no IO.
  Keep them that way. They hold the logic most likely to be wrong and are the
  cheapest to test.
- All DuckDB calls are blocking. Dispatch them through `asyncio.to_thread` so the
  MCP event loop is never blocked. Serialize writes behind an `asyncio.Lock`.
- pytest, function-style tests, Hypothesis for the correctness property.

## Architecture

```
MCP client (agent)
      | stdio
   server.py      tools/list = union of downstream tools + query
      |
   proxy.py       client sessions to downstream servers, call forwarding
      |
   shape.py       extract rows -> depth-1 projection      (pure)
   store.py       envelope row + typed table in DuckDB
   handle.py      preview + table name + columns -> back to the agent
   query.py       read-only SQL: statement gate, timeout, caps
```

Data model: one `sluice_calls` envelope row per call, always. Plus one typed
table per eligible call, named `<server>__<tool>__<seq>`, never appended to, with
a `<server>__<tool>__latest` view repointed after each call.

## Rules that are load-bearing

- **The DB is in-memory and dies with the process.** Deliberate. Do not add
  persistence without going back through `intent/`.
- **Sluice never turns a working tool call into a failed one.** Every
  materialization failure degrades to an envelope-only handle and logs. See
  spec §8.
- **The handle must ride in `content`.** `_meta` is not fed to the model by
  clients. `structuredContent` is a mirror on the way out, not the primary
  channel.
- **On the way in, `structuredContent` wins.** A downstream tool may put its data
  in `structuredContent` and a prose summary in `content`. Materializing the text
  in that case flattens the summary and discards the data. Channel priority is
  spec §5.1 step 3, and the chosen channel is always reported.
- **`max_payload_bytes` is a hard ceiling, not a size gate.** Peak memory during
  materialization is a multiple of payload size and an OOM kill ends the session,
  which is the one failure the "never fails a working call" invariant cannot
  absorb. Everything under the ceiling is still intercepted.
- **Read-only means two layers, not a SELECT prefix check.** DuckDB's own
  `extract_statements` parser for the statement gate, plus
  `enable_external_access = false` and `lock_configuration = true` at session
  open. `SELECT * FROM read_csv('/etc/passwd')` is a SELECT.
- **Every truncation is reported to the agent.** A silently truncated result is a
  correctness bug in a tool that sells determinism.
- **`sample_size = -1` on every `read_json`.** Sampled type inference breaks on a
  column whose type changes late in the payload.

## Out of scope for v0

No cross-session persistence, no cross-server joins or entity resolution, no
auth, policy, redaction, or audit layer, no hosted service, no UI. These are
recorded as choices in `intent/001-scratch-db.md` §"Non-goals", not as backlog.
