# Changelog

This tracks what has landed against the milestones in
`plan/001-scratch-db.md`.

## 0.1.0 — 2026-09-01

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

### M5 — correctness property

- The end-to-end Hypothesis suite covers normalized aggregate correctness,
  1–500-row boundaries, bounded floating-point regression evidence, and the
  mandatory counterexamples. Adversarial review found cancellation inside the
  former floating-point guarantee, so every `DOUBLE` column is now marked
  inexact and per-aggregate exactness is left for future design.
- The reproducible live-model median demo is implemented outside CI. Its
  committed sampled run recorded a wrong baseline answer (71.5) and the exact
  Sluice-backed answer (72.5), with transcripts and a non-determinism warning.

### Runtime bounds and adversarial review

- Whole-pipeline admission, deterministic logical session retention, and
  metadata/catalog cardinality limits bound Sluice's own continued work.
- Post-fix calibration across flat, nested, wide, and mixed 1 MiB dual-channel
  payloads at concurrency 2 supports the conservative 1 MiB v0 admission
  default; a 15-call run records long-session behavior.
- Final review closed CTE binding-order isolation, scalar memory amplification,
  queued-worker timeout, truncation-reporting, dual-channel sizing, failure
  envelope, and dead-transport reuse defects.

### M6 — documentation and packaging

- Installation, client configuration, architecture, usage, exactness,
  isolation, resource bounds, limitations, troubleshooting, and release notes
  are documented. The wheel includes `py.typed` and is validated by CI.

### Release verification

- The end-to-end canary against a real configured GitHub MCP server passed; see
  `review/canary-v0.1.0.md` for the observed handle, query, envelope, and
  isolation checks.
- The distribution name is `mcp-sluice`; the import package and console command
  remain `sluice`.
- Source and distributions are licensed under Apache-2.0.
