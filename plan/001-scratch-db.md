# Plan 001: Scratch DB for MCP tool results

Status: draft (revision 3, post-M0 and post-review)
Date: 2026-08-28
Implements: spec/001-scratch-db.md
Evidence: plan/001-notes-m0.md

Read the spec first. This document names files, order, risks, and tests, and does
not restate design. It assumes no exposure to the conversation that produced it.

**M0 is complete.** Its results are in `plan/001-notes-m0.md` and are load-bearing
throughout. Pinned: CPython 3.14.2, DuckDB 1.5.5, `mcp` 2.1.1, MCP protocol
revision `2026-07-28`.

## 0. Repository layout

```
repos/sluice/
├── pyproject.toml
├── CLAUDE.md
├── README.md
├── sluice.example.toml
├── intent/001-scratch-db.md
├── spec/001-scratch-db.md
├── plan/001-scratch-db.md
├── plan/001-notes-m0.md
├── review/codex-prompt.md
├── src/sluice/
│   ├── __init__.py
│   ├── __main__.py          # entrypoint: sluice [--config path]
│   ├── config.py            # TOML load, ${VAR} expansion, validation
│   ├── models.py            # dataclasses: Handle, CallRecord, TableRef, Limits
│   ├── naming.py            # injective naming, quoting, collisions (§3.2)
│   ├── scope.py             # scope id derivation and minting (§12)
│   ├── payload.py           # eligibility and channel selection (§5.1). Pure.
│   ├── intercept.py         # record, then replace the payload with a handle
│   ├── proxy.py             # downstream sessions, paginated listing, MRTR relay
│   ├── errors.py            # the four failure classes (§8)
│   ├── server.py            # MCP server: tools/list, tools/call, control plane
│   ├── shape.py             # extraction (§5.2), depth-1 projection (§5.3)
│   ├── infer.py             # column types and the exact flag (§5.5)
│   ├── store.py             # DuckDB: lockdown, envelope, table creation
│   ├── handle.py            # handle rendering, both channels (§4)
│   └── query.py             # three-layer gate, per-query connection, shaping
└── tests/
    ├── conftest.py
    ├── fake_server/__main__.py
    ├── test_naming.py       ├── test_scope.py       ├── test_shape.py
    ├── test_intercept.py
    ├── test_infer.py        ├── test_store.py       ├── test_handle.py
    ├── test_proxy.py        ├── test_errors.py      ├── test_query_safety.py
    ├── test_query_limits.py ├── test_passthrough.py ├── test_concurrency.py
    ├── test_end_to_end.py   └── test_property_aggregates.py
```

`shape.py`, `infer.py`, and `naming.py` are pure: no DuckDB, no MCP, no IO. They
hold the logic most likely to be wrong and are cheapest to test in isolation.

`platform.toml` gains a minimal `[projects.sluice]` entry (`repo_dir`,
`worktree_prefix = "wt/sluice-"`, `default_branch`). No `ghcr_image`, no
`infra_service`, no `domains`: Sluice is a local process.

## 1. Order of work

### M1. Proxy, no database

`config.py`, `naming.py`, `proxy.py`, `errors.py`, `server.py`, `__main__.py`,
`tests/fake_server/`.

Sluice starts, connects to one downstream server over stdio, mounts its tools,
and forwards calls with results returned unmodified. No DuckDB yet.

Four things here are not obvious and each has a test that fails without them:

1. **Paginated listing.** `ClientSession.list_tools()` returns one page and does
   not follow `next_cursor` (verified, SDK 2.1.1). Loop until `next_cursor is
   None`, treating `""` as a valid cursor.
2. **Whole-object tool cloning.** Clone the downstream `Tool` and mutate only
   `name`, `description`, `outputSchema`. Field-by-field reconstruction drops
   `annotations`, `title`, `icons`, `_meta`.
3. **MRTR relay.** `InputRequiredResult` out, `input_responses` and
   `request_state` in, both untouched. Advertise no sampling or elicitation
   capability downstream so Sluice is never asked to answer for the human.
4. **The error taxonomy** (spec §8). `validate_tool_result` raises `RuntimeError`
   rather than returning a result, so "verbatim passthrough" is impossible for
   three of the four classes. It is classified by message substring, which
   `test_engine_contract.py` pins.
5. **Connect with `mcp.Client`, call through `client.session`.** Found while
   building: `InputRequiredResult` exists only at protocol `2026-07-28`, and the
   `initialize` handshake tops out at `2025-11-25`. Only the `Client` connect
   path probes `server/discover` and reaches the modern version. Calls still go
   through the underlying session so round trips come back to Sluice for
   relaying rather than being resolved inside the SDK. See spec §11.

Build the fake downstream server here. It is the fixture for everything after.

| Tool | Emits |
|---|---|
| `rows(n)` | `{"items": [...n homogeneous objects...]}`, seeded, with a numeric `score` |
| `bare_rows(n)` | a bare JSON array of objects |
| `mixed(n)` | varying key sets; a type that changes at row 300 |
| `nested()` | nested objects and arrays |
| `wide(k)` | k top-level keys, all equally present, for the cap tie-breaker |
| `scalars(n)` | a bare array of numbers |
| `just_text()` | non-JSON prose |
| `one_object()` | a single JSON object |
| `empty()` | `{"items": []}` |
| `boom()` | `isError: true` |
| `picture()` | an image content block |
| `two_arrays()` | `{"rows": [...20], "facets": [...100]}` |
| `mixed_elements()` | a list of 9 objects and 1 scalar |
| `structured_only()` | data in `structuredContent`, prose in `content` |
| `both_channels()` | both populated and **disagreeing** |
| `two_text_blocks()` | two blocks each holding valid JSON |
| `bad_schema()` | declares an `outputSchema`, returns non-conforming output |
| `needs_input()` | returns `InputRequiredResult`, completes on the second round |
| `hyphen-tool` / `hyphen_tool` | two tools colliding under naive sanitizing |
| `paged()` | forces a two-page `tools/list` with a tool only on page 2 |
| `edge_numbers()` | int64 boundary, ±2^53 boundary, non-finite floats |

Exit criterion: `test_proxy.py` and `test_passthrough.py`. Passthrough compares
**parsed SDK models, not bytes**: the SDK deserializes and reserializes, so key
order and escaping may differ while the result is unchanged.

### M2. Envelope, scope, handle  [COMPLETE]

`scope.py`, `store.py`, `models.py`, `payload.py`, `handle.py`, `intercept.py`.

In-memory DuckDB at startup with the §6.1 lockdown, `sluice_calls`, scope
derivation (client `_meta` conversation id when present, minted per call
otherwise), envelope row per call, handle replacing eligible results. Channel
selection and conflict detection (§5.1). Passthrough exceptions and the
complete-preview rule.

Exit criterion: `test_store.py`, `test_scope.py`, `test_handle.py`. The handle
carries `channel`, `scope`, every table with its path, and renamed-column
mappings, all of which revision 2 promised in prose and omitted from the rendered
output. `both_channels()` sets `channel_conflict` and the handle says so.

### M3. Flattening and inference  [COMPLETE]

`shape.py`, `infer.py`, table creation in `store.py`.

Extraction (§5.2) including materializing **every** candidate array, depth-1
projection, Sluice's own inference with the `exact` flag (§5.5), file-free
creation via explicit DDL plus `executemany`, `_row` and `_call_id` injection,
recursive collision renaming, column cap with a deterministic tie-breaker,
no `__latest` view (spec §3.2, removed in revision 4).

M0 forced inference into scope: the §6.1 lockdown blocks `read_json`, so DuckDB
cannot do it. Budget accordingly. This is the largest change between the
pre-spike and post-spike plan.

Exit criterion: `test_infer.py` covers §5.5 row by row and pins the three
silent-wrongness cases: mixed scalars become `VARCHAR` not `JSON`, ISO-8601
strings stay `VARCHAR`, and a mixed int-and-float column with an integer past
2^53 is marked non-exact. `test_shape.py` covers every fake-server tool;
`two_arrays()` yields **two** tables and `mixed_elements()` yields none.

### M4. The `query` tool

`query.py`. Three-layer gate, per-in-flight-query connection, watchdog interrupt
on that exact object, `fetchmany`, defined markdown escaping, character-safe
truncation, explicit truncation reporting.

Exit criterion: `test_query_safety.py` and `test_query_limits.py`. Rejection
table, all of which must be rejected or fail:

```
INSERT INTO sluice_calls VALUES (...)
CREATE TABLE evil AS SELECT 1
DROP TABLE sluice_calls
ATTACH '/tmp/x.db' AS x
COPY (SELECT 1) TO '/tmp/out.csv'
SELECT * FROM read_csv('/etc/passwd')
SELECT * FROM read_parquet('/etc/passwd')
SELECT * FROM glob('/etc/*')
PRAGMA database_list                      -- layer 2 does NOT stop this
INSTALL httpfs
SELECT * FROM duckdb_tables()             -- layer 3
SELECT * FROM information_schema.tables   -- layer 3
SET enable_external_access = true
SELECT 1; DROP TABLE sluice_calls
```

And these must **succeed**, because rejecting legal SQL is its own defect:
`SELECT 1;` with a trailing semicolon, a query with duplicate output column
names, `WITH x AS (...) SELECT ...`, and `SELECT 1 -- ; comment`.

### M5. Correctness property and demo

`test_property_aggregates.py`, `test_end_to_end.py`, `demo/`. See §3.

### M6. Documentation

`README.md`, `sluice.example.toml`, `CLAUDE.md` reconciled against what was built.

## 2. Risks

**R1. MCP SDK surface.** Retired. M0 step 5 established the low-level
`Server(on_list_tools=..., on_call_tool=...)` API and per-call
`structuredContent` and `isError`. Note `mcp.server.fastmcp` no longer exists in
2.1.1; the former surface is `mcp.server.mcpserver.MCPServer`.

**R2. DuckDB cp314 wheel.** Retired. DuckDB 1.5.5 ships one.

**R3. Sluice owns type inference.** Severity: high. Forced by the lockdown
(spec §5.4). Every inference bug is now ours, and inference bugs produce a
confident, well-typed, wrong answer. Mitigation: `infer.py` is pure and directly
tested, the property test fuzzes it, and §5.5 prefers `VARCHAR` and a loud
failure over a clever type and a quiet one.

**R4. The correctness criterion is domain-bounded.** Severity: high. It is exact
only inside §5.6's domain. If the `exact` flag is wrong for a column, the
guarantee lapses without anyone noticing. Mitigation: the counterexamples in
§5.6 are mandatory test cases, not generated ones, because Hypothesis on small
values will never find them.

**R5. Scope isolation is capability-based, not enforced.** Severity: medium and
accepted. Without a client-supplied conversation id it rests on unguessable table
names plus a catalog denylist, and denylists are weaker than the other two query
layers. Spec §12 states the residual risk. The unconditional half, that a stale
handle cannot resolve to live data holding different contents, does hold.

**R6. Peak memory is a multiple of payload size.** Severity: high. Measured 9.72×
RSS on the discarded file-based pipeline; the file-free pipeline has **not** been
re-measured and must be before `max_payload_bytes` is trusted. `structuredContent`
is already decoded by the SDK before the check, so the ceiling bounds what Sluice
does next, not what already happened. `max_concurrent_materializations` gates the
whole pipeline, not just the write.

**R7. MRTR relay is stateful.** Severity: medium, new in v0 scope. Relaying
`request_state` means Sluice carries an in-flight call across round trips. The
hazard it prevents is worse: a high-level client would have Sluice answering
elicitations addressed to the human. Mitigation: `needs_input()` in the fake
server, and advertising no sampling or elicitation capability downstream.

**R8. `JSON` columns are aggregation traps.** Severity: medium. `sum()` and
`avg()` raise a binder error, which is loud, but `median()` succeeds and returns a
lexicographic result: over integers 0 to 299 plus one string it returned `'232'`.
§5.5 avoids producing `JSON` for mixed scalars, and the handle marks genuine
`JSON` columns, but an agent can still write `median(json_col)` on a legitimately
nested column and get a number-shaped lie.

**R9. Static tool catalog.** Severity: low, declared. Sluice advertises
`listChanged: false`; a downstream server adding tools at runtime needs a Sluice
restart. Stated in spec §10 rather than left as a surprise.

**R11. Protocol version regression.** Severity: medium. Round trips need
`2026-07-28`, reached only via the `Client` discover probe. A refactor to a bare
`ClientSession` would silently drop to `2025-11-25`, and the symptom is a
serializer validation error naming neither the tool nor the version.
`test_engine_contract.py` pins the version facts; `test_proxy.py` pins the relay.

**R12. A green suite that is not a gate.** Severity: high, and it fired once
already. After M3, all 148 tests passed against builds that stored every `_row`
as 0, stored the wrong `_call_id`, dropped downstream `structuredContent`,
discarded result `_meta`, and derived scopes from a process-randomized hash. The
suite asserted schemas and counts but almost never asserted stored values.
A second review round found three more survivors after those fixes: whole-model
comparisons covered `Proxy.call` but not the rest of the product path, so
stripping result `_meta` in the interceptor or the upstream server still passed;
a degraded envelope could keep `source_paths` for tables that were rolled back;
and a fault-injection test ended in `assert intercept_module is not None`, which
asserts nothing.

Mitigation: full-record assertions rather than field subsets, whole-model
comparison at the **product** boundary and not only at an internal seam, fault
injection at each write boundary, and a mutation pass as a standing step rather
than a one-off. Coverage alone does not catch this class; every one of those
mutants ran covered lines. Watch for assertions that cannot fail: a test whose
last line is `assert <import> is not None` is a placeholder wearing a test's
clothes.

**R10. Downstream pagination of results.** Severity: low. A tool that pages
produces N tables the agent must UNION by hand. README note, not code.

## 3. Tests that prove it

### The correctness property

`test_property_aggregates.py`, Hypothesis plus a fixed counterexample table.

Generate lists of dictionaries with mixed types, missing keys, and 1 to 500 rows.
Serve from the fake server, call through Sluice, then compare against a Python
reference computed over the **§5.3 normalization**, not raw JSON: missing key and
JSON `null` both become `None`, and the reference skips `None` exactly as SQL
skips `NULL`.

Restrict generated numeric columns to §5.6's safe domain. A mixed int-and-string
column becomes `VARCHAR` by design and asserting a numeric aggregate over it
tests the wrong thing.

**Exact:** `count(*)`, `count(col)`, `min`, `max`, `count(DISTINCT)` against
`len({v for v in col if v is not None})`, `GROUP BY` counts against
`collections.Counter`, integer `sum`, and `median` against `statistics.median`.

**Within tolerance:** `avg` and float `sum`, via `math.isclose(rel_tol=1e-9,
abs_tol=1e-12)`. Both bounds are required; relative tolerance alone does not
survive cancellation.

**Mandatory fixed cases**, outside the domain, asserting the `exact` flag is
false and no exactness is claimed. Hypothesis will not generate these:

```
median([0, 9223372036854775806, 9223372036854775807])  duck 9.223372036854776e+18  py exact int
median([1e308, 1e308])                                 duck 1e308                  py inf
avg([-1e308, 1.0, 2.0, 1e308])                         duck 0.0                    py 0.75
max([9007199254740993, 0.5])                           duck 9007199254740992.0     py exact int
```

### Everything else

| File | Proves |
|---|---|
| `test_engine_contract.py` | the measured DuckDB and SDK behaviors the design rests on, asserted against installed versions; the tripwire that makes unpinned dependencies safe |
| `test_naming.py` | injectivity: `a-b` vs `a_b` and `Foo` vs `foo` get different tables; 128-char mounted-name validation fails startup loudly; sequence widening past 9999; identifier quoting |
| `test_scope.py` | scope from client `_meta` when present, minted otherwise; a stale handle from a prior process cannot resolve to a live table; a fixed BLAKE2 digest vector; cross-process stability under three `PYTHONHASHSEED` values; minting draws from `secrets`; 128-bit width |
| `test_config.py` | every rejection path; limit value AND type validation (TOML supplies strings, floats, and bools that reached the comparisons uncaught); env expansion; file loading; `find_config` precedence; the shipped example config loads |
| `test_cli.py` | a real `python -m sluice` subprocess serving over stdio and returning a handle; exit codes and clean diagnostics for a missing config, invalid limits, and an unreachable downstream; startup errors unwrapped from anyio ExceptionGroups |
| `test_shape.py` | extraction for every payload shape; every candidate array materialized; `mixed_elements` and `empty` reported rather than degraded; depth-1 projection; missing key and JSON `null` both NULL; reserved-column collisions allocated recursively; column cap with a deterministic tie-break |
| `test_infer.py` | every row of §5.5; mixed scalars `VARCHAR` not `JSON`; ISO-8601 stays `VARCHAR`; int128 gets `HUGEINT`; the ±2^53 rule and non-finite floats set `exact: false`; bool checked before int; value coercion |
| `test_store.py` | lockdown closes external access while Sluice can still create tables; envelope round-trips including JSON columns; per-tool monotonic sequence |
| `test_intercept.py` | payload replaced by a handle; structured mirror; structured channel beats prose; conflict surfaced; `not_json`; complete vs truncated preview; errors and images pass through and are still recorded; image bytes not stored; oversize passes through unparsed with payload columns NULL; no query hint before M4; a forced envelope-write failure returns the downstream result (FR-8) |
| `test_handle.py` | handle carries channel, scope, every table with path, renamed columns, and the `exact` flags; `structuredContent` mirrors it; complete-preview rule below budget; `channel_conflict` surfaced |
| `test_proxy.py` | `paged()` mounts the page-2 tool; the whole tool object survives cloning including `annotations.destructiveHint`; `needs_input()` completes across a relayed round trip; Sluice advertises no sampling or elicitation capability |
| `test_errors.py` | all four failure classes produce distinct `failure_class` values and correct upstream results; `bad_schema()` produces an `output_schema` failure rather than an unhandled `RuntimeError` |
| `test_query_safety.py` | the M4 rejection table including layer-3 catalog blocks; the must-succeed list |
| `test_query_limits.py` | row cap reported as "additional rows exist" and never as a count; byte and cell caps; timeout; character-boundary truncation on multi-byte UTF-8; defined markdown escaping for `|`, newlines, NULL, and empty string |
| `test_passthrough.py` | whole parsed model equal to a direct call for every shape; result `_meta` and content annotations survive; the same asserted through the **full product path** (`build_server` + interceptor), not only `Proxy.call`; oversize passthrough leaves payload columns NULL |
| `test_concurrency.py` | a timed-out `query` leaves a concurrent write intact and its own connection closed; two concurrent queries do not interrupt each other; `max_concurrent_materializations` actually gates parsing |
| `test_end_to_end.py` | full stdio loop: list tools, see the appended description and absent `outputSchema`, call `rows(400)`, get a handle, `query` a median, get the right number |

### Not in CI

The demo eval. It calls a model, it is non-deterministic, and gating CI on model
behavior makes the suite lie.

## 4. Definition of done for v0

1. `uv run sluice --config sluice.toml` runs as an MCP stdio server against one
   downstream server.
2. `pytest` green, including the property test and its four fixed counterexamples.
3. The M4 rejection table fully covered, and the must-succeed list passing.
4. `max_payload_bytes` re-measured against the file-free pipeline (R6).
5. The demo produces a wrong median without Sluice and the right one with it, on
   the same 400 rows, transcript saved into the repo.
6. `CLAUDE.md` accurate against the built code.
