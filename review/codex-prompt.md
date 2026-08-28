# Codex review prompts

Two passes, deliberately separate. Pass 1 is empirical and produces a committed
artifact. Pass 2 is adversarial and reads that artifact. Combining them was the
main weakness of the first version of this file: one prompt was carrying a
compatibility spike, protocol research, security analysis, concurrency testing,
and design review at once.

Launch with `./bin/platform agent codex` from the workspace root.

---

## Pass 1: empirical spike

Execute the M0 verification spike in `repos/sluice/plan/001-scratch-db.md` §1,
all eight steps, and write the results to `repos/sluice/plan/001-notes-m0.md`.

Read `repos/sluice/spec/001-scratch-db.md` first for context on why each step
matters. Sluice is a passthrough MCP server that materializes downstream tool
results into a session-scoped DuckDB and returns a preview plus a table handle
instead of the payload, with one `query` tool for read-only SQL over those
tables. Python 3.14+, official MCP Python SDK, DuckDB. No code exists yet.

Rules for this pass:

- **Run everything. Do not reason from documentation.** Install the packages,
  execute the code, report what happened. A claim about DuckDB or SDK behavior
  that you did not observe is worthless here.
- Record exact resolved versions of `duckdb`, `mcp`, and CPython.
- For each step, include the code you ran and the raw output, so the notes are
  reproducible by someone else.
- Where a step's result contradicts the spec, say so plainly and name the spec
  section. Do not fix the spec in this pass.
- Steps 1, 4, and 6 can each change the architecture rather than refine it. Give
  them the most care. Step 4 in particular: the basic "interrupt stops the query"
  result is not the interesting one, the isolation behavior is.

Output: the notes file, plus a short list of any spec section that the results
contradict. Nothing else.

---

## Pass 2: adversarial design review

Run only after `plan/001-notes-m0.md` exists.

You are reviewing the design of Sluice before implementation. There is no code.
Read all five documents in `repos/sluice/`:

- `intent/001-scratch-db.md`
- `spec/001-scratch-db.md`
- `plan/001-scratch-db.md`
- `plan/001-notes-m0.md` (the empirical results from pass 1)
- `CLAUDE.md`

The project's correctness criterion: an aggregate computed via the `query` tool
must equal the same aggregate computed directly over the raw source data.

### Part A: the design against the empirical results

Where do the spec and plan still assert something that pass 1 disproved or left
unresolved? Every such case is a blocking finding.

### Part B: the design against the MCP specification

Verify against the current spec revision and the SDK source, and say which of
these are normative and which are only convention:

- Is `structuredContent` required to conform to a tool's declared `outputSchema`?
- Is it legal for a tool with no `outputSchema` to return `structuredContent`?
  Spec §4.3 removes downstream `outputSchema` from mounted tool definitions
  specifically to make this legal.
- Is `_meta` on a tool result surfaced to the model by clients, or is it
  programmatic only? Spec §4.1 asserts `content` is the only channel guaranteed
  to reach the model, and the whole handle design rests on that.
- Anything in the proxying model (§2, FR-1 through FR-6) that conflicts with the
  spec: capability negotiation, tool list change notifications, cancellation,
  progress, pagination of `tools/list`.

### Part C: adversarial review

Assume this gets built exactly as written. What breaks, and where is the spec
underspecified enough that two competent implementers would build different
things?

Sharp edges to check specifically:

1. **Aggregate semantics.** The flagship property test asserts DuckDB aggregates
   equal Python `statistics` results. Verify that DuckDB `median()` matches
   `statistics.median` for even and odd row counts, integer and float columns,
   and that `avg` matches `statistics.fmean` within a stated tolerance. Float
   `sum` associativity may differ. If these do not match exactly, the project's
   own correctness criterion is wrong as written and the spec needs a defined
   tolerance and a defined reference implementation.
2. **NULL handling** in that property test. SQL aggregates skip NULLs and the
   Python reference must skip them the same way. Where else does SQL NULL versus
   missing key versus JSON `null` get conflated in the depth-1 projection?
3. **Channel priority** (§5.1 step 3). Is `structuredContent` over text the right
   order in every case? What about a tool where the two disagree?
4. **The extraction heuristic** (§5.2): picking the longest array-of-objects from
   a top-level object. Where does this pick wrong on real MCP payloads, and is
   reporting `source_path` in the handle actually sufficient mitigation?
5. **`max_payload_bytes`** (§5.1 step 2, §8). Is a pre-parse byte ceiling the
   right instrument, is 32 MB defensible against the pass-1 memory measurement,
   and does the passthrough-with-a-size-note behavior leave the agent able to do
   anything useful?
6. **Identifier safety.** `<server>__<tool>__<seq>` built from
   downstream-controlled tool names: SQL identifier injection, length limits,
   collisions after case folding and sanitizing, `_extra` and `_row` collisions.
7. **Concurrency** (§9) and the `__latest` view repointing under concurrent calls
   to the same tool.
8. **The plan's test matrix.** Name specific scenarios the listed tests would
   pass while the system is broken.

### Decisions that are settled

Do not relitigate these. You may flag one **only** if it is internally
inconsistent with something else in the spec, in which case name both sides.

1. Depth-1 flattening with JSON columns for nested values, rather than native
   DuckDB STRUCT/LIST inference.
2. The handle rides in `content`, mirrored into `structuredContent`, never
   `_meta` alone.
3. Always intercept, no size gate. `max_payload_bytes` is a memory ceiling and is
   not a reopening of this.
4. No separate `fetch` tool. Full payload recovery goes through `query` against
   the envelope table.
5. Errors and non-text content pass through verbatim.
6. Proxied tool descriptions get one appended sentence.
7. A purpose-built in-repo fake downstream server is the test fixture.
8. No cross-session persistence, no cross-server joins, no auth, policy,
   redaction, or audit layer, no hosted service, no UI, in v0.

Open by design, comment freely: whether the Python floor moves to 3.15 (gated on
DuckDB cp315 wheel availability), multi-server fan-out, pagination across
repeated calls to the same tool, and table discovery beyond in-context handles.

### Output format

A numbered list of findings, most severe first. Each finding:

- **Where**: file and section number.
- **Severity**: blocking, significant, or minor.
- **Claim**: what the document says.
- **Failure**: the concrete case where it is wrong or ambiguous, with inputs.
- **Fix**: the specific change to the document.

No summary of what the documents say. No praise. No restating the design back at
me. If a section is fine, say nothing about it. Say "no actionable issues" only
if that is true.

This is a multi-round review. Expect to be asked again after fixes.
