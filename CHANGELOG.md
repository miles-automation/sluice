# Changelog

No version has been tagged or released. This tracks what has landed against the
milestones in `plan/001-scratch-db.md`, for anyone picking up the repo mid-build.

## Unreleased — v0, M1 through M4

- **M1 — proxy.** Sluice starts, connects to one configured downstream MCP
  server over stdio, mounts its tools under injective names, and forwards
  calls unmodified. Paginated `tools/list`, whole-object tool cloning,
  multi-round-trip relay, and the four-class failure taxonomy (§8).
- **M2 — envelope, scope, handle.** Every proxied call gets one `sluice_calls`
  row in an in-memory DuckDB database, under the §6.1 engine lockdown. Scope
  is derived from a client-supplied conversation id when present, minted
  per call otherwise. Eligible results are replaced with a handle; channel
  selection and conflict detection between `structuredContent` and text
  content are implemented per §5.1.
- **M3 — flattening and inference.** Depth-1 projection extracts every
  candidate row set from a result (§5.2), Sluice's own type inference assigns
  a column type and an `exact` flag per §5.5 without going through DuckDB's
  JSON inference, and tables are created file-free via explicit DDL plus
  `executemany`.
- **M4 — the `query` tool.** The three-layer read-only gate (statement type,
  engine lockdown, AST object allowlist), a per-query DuckDB connection with a
  timer-based interrupt, and defined result shaping (row cap, byte cap,
  per-cell truncation, markdown escaping) are implemented and covered by
  `test_query_safety.py` and `test_query_limits.py`. Two review passes closed
  a gate bypass and several other failures after initial implementation (see
  commit history).
- Full test suite passes: proxy, passthrough, envelope, scope, shape,
  inference, store, handle, config, CLI, engine-contract, query safety,
  query limits, and concurrency.

### Not yet done (M5, M6)

- The Hypothesis-based correctness property test and its fixed
  outside-the-domain counterexamples do not exist yet.
- The model-eval demo (`demo/median`) comparing a wrong in-context median to
  an exact `query`-computed one does not exist yet, and is explicitly out of
  CI regardless (it calls a live model and is non-deterministic).
- `max_payload_bytes` has not been re-measured against the file-free
  materialization pipeline (plan R6).
- CI is in place; there is no license file or tagged release yet.
