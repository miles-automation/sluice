# Plan 001: Scratch DB for MCP tool results

Status: draft
Date: 2026-08-28
Implements: spec/001-scratch-db.md

This plan assumes no prior exposure to the conversation that produced the intent
and spec. Read spec/001-scratch-db.md first; this document names files, order,
risks, and tests, and does not restate design.

## 0. Repository layout

`git init` first. `repos/sluice/` is currently a documentation-only directory and
is not yet a repository, so none of the artifact chain is under version control.
Commit intent, spec, and plan as the first commit, before any code, so the
document history is separable from the implementation history.

New repo at `repos/sluice/`, standard workspace location. Add a minimal entry to
the workspace `platform.toml` so tooling discovers it (`repo_dir`,
`worktree_prefix = "wt/sluice-"`, `default_branch`). No `ghcr_image`, no
`infra_service`, no `domains`: Sluice is a local process, not a deployed service.

```
repos/sluice/
├── pyproject.toml
├── CLAUDE.md
├── README.md
├── intent/001-scratch-db.md
├── spec/001-scratch-db.md
├── plan/001-scratch-db.md
├── sluice.example.toml
├── src/sluice/
│   ├── __init__.py
│   ├── __main__.py          # entrypoint: sluice [--config path]
│   ├── config.py            # TOML load, ${VAR} expansion, validation
│   ├── models.py            # dataclasses: Handle, CallRecord, FlatResult, Limits
│   ├── proxy.py             # downstream client sessions, tool list union, call forwarding
│   ├── server.py            # the MCP server: tools/list, tools/call, wiring
│   ├── store.py             # DuckDB session: open, lockdown, envelope, table creation
│   ├── shape.py             # extraction (§5.2) and depth-1 projection (§5.3). Pure functions.
│   ├── handle.py            # handle rendering: content text + structuredContent
│   ├── query.py             # the query tool: statement gate, timeout, result shaping
│   └── naming.py            # namespacing, table names, identifier sanitizing, collisions
└── tests/
    ├── conftest.py
    ├── fake_server/
    │   ├── __init__.py
    │   └── __main__.py      # the in-repo fake downstream MCP server
    ├── test_shape.py
    ├── test_naming.py
    ├── test_store.py
    ├── test_handle.py
    ├── test_query_safety.py
    ├── test_query_limits.py
    ├── test_passthrough.py
    ├── test_end_to_end.py
    └── test_property_aggregates.py
```

`shape.py` and `naming.py` are pure and have no DuckDB or MCP imports. That is
deliberate: the two places most likely to be wrong are the ones cheapest to test
in isolation.

## 1. Order of work

Each milestone ends in a state that runs and is tested. Do not proceed past M0
until it is done, because M0 can invalidate the stack.

### M0. Verification spike (do this first, throw the code away)

Nothing below is worth writing if these do not hold. Half a day at most.

1. In a **fresh temporary environment**, `uv pip install --only-binary=:all: duckdb mcp`
   succeeds on CPython 3.14. The `--only-binary` flag is the point: an install
   that succeeds by falling back to a source build proves nothing about wheel
   availability. Record both resolved versions. If DuckDB has no cp314 wheel,
   stop and escalate: the Python floor becomes the blocking decision, not an open
   question.
2. `con.extract_statements("SELECT 1")` returns one statement whose `.type` is
   `StatementType.SELECT`, and `extract_statements("INSERT INTO t VALUES (1)")`
   reports a different type. Confirms the §6.1 layer-1 gate is buildable as
   specified.
3. After `SET enable_external_access = false; SET lock_configuration = true`,
   confirm that `SELECT * FROM read_csv('/etc/hosts')` fails and that a
   subsequent `SET enable_external_access = true` also fails.
4. `con.interrupt()` from another thread actually aborts a long-running query on
   a cursor derived from that connection. Time it. **Then test isolation, which
   matters more than the basic case:** run a long `query` and a concurrent
   materialization write on two cursors from the same connection, interrupt the
   query, and report (a) whether the write survived, (b) whether the connection
   is usable afterward, and (c) whether uncommitted state was lost. If the
   interrupt is connection-wide, spec §9's single-connection model is unsafe and
   `query` needs its own connection. Repeat the experiment with two separate
   connections to the same in-memory database to confirm that isolation works.
5. In the MCP Python SDK at the installed version: confirm how to serve a
   **dynamic** tool list (the tool set is discovered from downstream at startup,
   not decorated at import time) and how to return `structuredContent` and
   `isError` from a call handler. The low-level `mcp.server.Server` is expected
   to be the right surface rather than `FastMCP`, but verify rather than assume.
   Write down the exact call shape found; the rest of the plan depends on it.
6. **Payload channels.** Construct results in each shape and confirm the SDK's
   parsed model exposes them distinguishably: text-only JSON; `structuredContent`
   only with prose in `content`; both populated with the text being a
   serialization of the structured value; and two text blocks each holding valid
   JSON. Spec §5.1 step 3 defines the priority order; confirm the SDK actually
   lets Sluice see which channel is which.
7. **Peak memory under a large payload.** Run a 50 MB JSON array through the full
   materialization pipeline and measure peak memory with `tracemalloc` and
   `resource.getrusage`. Report the multiple of payload size. Spec §5.1 step 2
   sets `max_payload_bytes` at 32 MB on an assumption about that multiple; this
   measurement either supports the number or replaces it. Confirm the NDJSON temp
   file is deleted on both the success and the failure path.
8. `read_json` with `format='newline_delimited'`, `union_by_name=true`,
   `sample_size=-1` over NDJSON where rows have different key sets and one column
   is integer for 300 rows then a string: does it load, and what type does that
   column get? Also check what happens to timestamp-shaped strings, integers
   beyond int64, and decimal-shaped strings. Spec §5.4 claims full-scan inference
   removes the drift failure mode; §5.3 claims the handle must report the type
   DuckDB actually assigned rather than the one predicted.

Record findings in `plan/001-notes-m0.md`, committed. This file is an input to
the design review that follows, which is why the empirical pass runs first and
saves its results rather than folding into one combined review.

If any of these fail, revise the spec before writing production code. Steps 1, 4,
and 6 can each change the architecture rather than refine it.

### M1. Transparent proxy, no database

`config.py`, `naming.py`, `proxy.py`, `server.py`, `__main__.py`,
`tests/fake_server/`.

Sluice starts, connects to one downstream server over stdio, mounts its tools
under `<server>__<tool>`, and forwards calls with results returned **unmodified**.
No DuckDB anywhere yet.

Build the fake downstream server here, not later. It is the test fixture for
everything after this point. It exposes:

| Tool | Emits |
|---|---|
| `rows(n)` | `{"items": [...n homogeneous objects...]}` with a numeric `score` field, seeded and deterministic |
| `bare_rows(n)` | a bare JSON array of objects |
| `mixed(n)` | objects with varying key sets and a type that changes at row 300 |
| `nested()` | objects containing nested objects and arrays |
| `wide(k)` | objects with k top-level keys, for the column cap |
| `scalars(n)` | a bare array of numbers |
| `just_text()` | non-JSON prose |
| `one_object()` | a single JSON object |
| `empty()` | `{"items": []}` |
| `boom()` | an `isError` result |
| `picture()` | a result with an image content block |
| `two_arrays()` | an object with two array-of-object fields of different lengths |

The fake server must also cover the payload-channel matrix from M0 step 6:
`structured_only()` (data in `structuredContent`, prose in `content`),
`both_channels()`, and `two_text_blocks()`.

Exit criterion: `test_passthrough.py` proves a call through Sluice returns a
result **semantically equal** to a call made directly against the fake server.
Compare the parsed SDK result models, not raw bytes. The SDK deserializes and
reserializes, so key order, unicode escaping, and whitespace may legitimately
differ while the result is unchanged. Asserting byte-identity would fail on
differences that do not matter and would tempt someone to weaken the test for the
wrong reason.

### M2. Envelope and handle

`store.py`, `models.py`, `handle.py`.

Open the in-memory DuckDB at startup, apply the §6.1 lockdown sequence, create
`sluice_calls` and `sluice_schema`. Write an envelope row per call. Replace
eligible results with an envelope-only handle. Implement the passthrough
exceptions (FR-11, FR-12) and the complete-preview rule (FR-13).

Exit criterion: `test_store.py` and `test_handle.py` pass. `boom()` and
`picture()` come back verbatim with envelope rows written. `just_text()` produces
an envelope-only handle. A small result's preview contains the whole payload and
says so.

### M3. Flattening

`shape.py`, plus the table-creation half of `store.py`.

Extraction (§5.2), depth-1 projection (§5.3), NDJSON load with
`union_by_name = true, sample_size = -1` (§5.4), `_row` and `_call_id` injection,
collision renaming, column cap and `_extra`, `__latest` view maintenance.

Exit criterion: `test_shape.py` covers every fake-server tool. `mixed(400)`
produces a union schema with NULLs and does not fail on the row-300 type change.
`wide(200)` produces 64 columns plus `_extra`. `two_arrays()` picks the longest
and reports the alternative in the handle. `empty()` reports `rows=0` rather than
disappearing.

### M4. The `query` tool

`query.py`.

Statement gate, engine lockdown assertions, watchdog timeout, row and byte caps,
markdown rendering, explicit truncation reporting.

Exit criterion: `test_query_safety.py` and `test_query_limits.py` pass. Safety
table, all of which must be rejected or must fail:

```
INSERT INTO sluice_calls VALUES (...)
CREATE TABLE evil AS SELECT 1
DROP TABLE sluice_calls
ATTACH '/tmp/x.db' AS x
COPY (SELECT 1) TO '/tmp/out.csv'
SELECT * FROM read_csv('/etc/passwd')
SELECT * FROM read_parquet('/etc/passwd')
PRAGMA database_list
SET enable_external_access = true
INSTALL httpfs
SELECT 1; DROP TABLE sluice_calls
SELECT 1 -- ; and a comment
```

The last two matter most: multi-statement input must be caught by statement
count, not by string splitting.

### M5. Correctness property and the demo

`test_property_aggregates.py`, `test_end_to_end.py`, and a `demo/` script.

The property test is the point of the project. See §3.

The demo is a separate script, not a test, and is not run in CI. It asks a model
for a median over roughly 400 rows twice, once with the raw payload in context
and once through Sluice, and prints both answers against the true value.

### M6. Documentation

`README.md`, `sluice.example.toml`, `CLAUDE.md` finalized against what was
actually built.

## 2. Risks

**R1. No DuckDB wheel for CPython 3.14.** Severity: blocking. DuckDB ships a
compiled extension module. A source build is not an acceptable install path for a
tool meant to be installed in one command. Detection: M0 step 1. Mitigation: this
converts the Python floor from an open question into a forced decision, and the
spec's §11.1 needs rewriting before any code is written.

**R2. MCP SDK surface for dynamic tools and structured output.** Severity: high.
The whole design rests on serving a tool list discovered at runtime and on
controlling `structuredContent` and `isError` per call. The SDK's ergonomic
surface (`FastMCP`) is oriented toward statically decorated tools. Detection: M0
step 5. Mitigation: drop to the low-level `Server` API. If neither surface
permits it cleanly, the handle degrades to content-only, which the spec already
identifies as a viable fallback (§4.2 becomes optional, §4.1 is untouched).

**R3. `extract_statements` behavior differs from expectation.** Severity: medium.
If statement types are not exposed as assumed, the fallback is `EXPLAIN <sql>`
plus a rejection of any input containing more than one statement, which is weaker
and worth knowing about early. Detection: M0 step 2.

**R4. `interrupt()` does not abort work on a derived cursor.** Severity: medium.
If it does not, the timeout must be implemented as one connection per in-flight
query instead of one shared connection with cursors, which changes §9's
concurrency model. Detection: M0 step 4.

**R5. Always-intercept degrades small calls.** Severity: medium, accepted.
Mitigated by FR-13, not eliminated. Watch for it in the demo: if the agent starts
issuing `query` calls for data that was already fully present in the preview, the
preview wording is not doing its job and needs to say more loudly that the data
is complete.

**R6. Type inference cost on large payloads.** Severity: low. `sample_size = -1`
scans every row. This is the right trade for correctness, and payload sizes in
scope are megabytes at most. If it shows up, the answer is a size threshold above
which inference falls back to sampling with a warning in the handle, not a
silently sampled inference.

**R7. The extraction heuristic picks the wrong array.** Severity: low, by design
visible. `two_arrays()` exists to pin this behavior. The mitigation is not a
better heuristic, it is that the handle always reports `source_path` and the
untouched payload is always reachable through `sluice_calls.result`.

**R8. A downstream server that streams progress or holds long-running calls.**
Severity: low for v0. Materialization happens on the final result only. Worth a
note in the README rather than code.

**R9. Payload arrives in `structuredContent`, not text.** Severity: high, and it
was a genuine blind spot in the first draft of the spec. A tool returning data in
`structuredContent` and a prose summary in `content` would have had its summary
flattened and its data discarded, behind a plausible-looking handle over the
wrong rows. Detection: M0 step 6 plus the `structured_only()` fake tool.
Mitigation: the channel priority in spec §5.1 step 3, with `source_channel`
reported so a wrong choice is visible rather than silent.

**R10. Peak memory is a multiple of payload size.** Severity: high. The pipeline
transiently holds the result object, concatenated text, parsed JSON, projected
rows, NDJSON, and the DuckDB table. `duckdb_max_memory` bounds only the last of
those. An OOM kill ends the session, so this is the one failure mode the §8
invariant cannot absorb. Detection: M0 step 7. Mitigation: `max_payload_bytes`
applied before parsing, and temp-file cleanup on every path.

**R11. The `query` SQL wrapper rejects valid SQL.** Severity: medium, now
avoided. Wrapping user SQL in `SELECT * FROM (<sql>) LIMIT n+1` breaks on
trailing semicolons and on result sets with duplicate column names, both of which
pass the §6.1 statement gate. Telling the agent its SQL is allowed and then
failing it is worse than the row cap the wrapper was solving. Resolved in spec
§6.3 by using `fetchmany` on the unmodified statement. Keep the wrapper's failure
cases as regression tests anyway.

## 3. Tests that prove it

### The correctness property (the one that matters)

`test_property_aggregates.py`, Hypothesis-generated.

Generate a list of dictionaries with a mix of integer, float, string, boolean,
and null values, missing keys on some rows, and between 1 and 500 rows. Serve
them from the fake server. Call through Sluice. Then for each numeric column,
assert that every one of these computed via `query` equals the same statistic
computed in pure Python over the generated source data:

- `count(*)` and `count(col)` against `len()` and non-null count
- `sum`, `min`, `max`
- `avg` against `statistics.fmean`, within floating point tolerance
- `median(col)` against `statistics.median`
- `count(DISTINCT col)` against `len(set(...))`
- `GROUP BY` a categorical column with counts, against `collections.Counter`

Null handling is the sharp edge: SQL aggregate semantics skip NULLs and the
Python reference must do the same, deliberately, not accidentally. Write the
reference implementation to skip them explicitly and comment why.

This is a property over generated data, so it also functions as a fuzzer for the
projection code in `shape.py`.

### Everything else

| File | Proves |
|---|---|
| `test_naming.py` | namespacing, identifier sanitizing, `_row`/`_call_id` and `_extra` collision renaming, collisions created by case folding, downstream tool names at MCP length limits, per-tool sequence numbering past 999 |
| `test_shape.py` | channel selection across the M0 step 6 matrix; extraction path selection for all payload shapes in the fake server; depth-1 projection; JSON columns for nested values; column cap and `_extra`; inference hazards (timestamp-shaped strings, integers beyond int64, decimal-shaped strings) reported with the type DuckDB actually assigned |
| `test_store.py` | one envelope row per call including errors and passthroughs; `flat_reason` populated on every table-less path; `__latest` view repoints; a forced load failure does not fail the call (FR-9) |
| `test_handle.py` | handle text contains table, row count, columns, `source_path`, call id; `structuredContent` mirrors it; complete-preview rule triggers below the budget and is labelled |
| `test_query_safety.py` | the §M4 rejection table; multi-statement input rejected by count |
| `test_query_limits.py` | row cap detected via the `n+1` fetch and reported; byte cap truncates and reports; timeout fires and reports; per-cell truncation; truncation cuts on character boundaries for multi-byte UTF-8; a trailing semicolon and a result set with duplicate column names both succeed (R11 regression) |
| `test_passthrough.py` | `isError` and image results semantically equal to direct downstream calls, compared as parsed models; oversize payloads pass through with a size note |
| `test_concurrency.py` | a timed-out `query` leaves a concurrent materialization write intact and the connection usable (R4, spec §6.2); `__latest` view repointing under concurrent calls to the same tool |
| `test_end_to_end.py` | full loop against the fake server over stdio: list tools, see the appended description sentence and the absent `outputSchema`, call `rows(400)`, get a handle, `query` a median, get the right number |

### Not in CI

The demo eval. It calls a model, it is non-deterministic, and gating CI on model
behavior makes the suite lie. It runs by hand, and its output is a claim about
the product rather than a check on the code.

## 4. Definition of done for v0

1. `uv run sluice --config sluice.toml` runs as an MCP stdio server against one
   configured downstream server.
2. `pytest` is green, including the Hypothesis property test.
3. The safety table in M4 is fully covered and every entry is rejected.
4. The demo script produces a wrong median without Sluice and the right one with
   it, on the same 400 rows, and that transcript is saved into the repo.
5. `CLAUDE.md` is accurate against the built code.
