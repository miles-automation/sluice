# Spec 001: Scratch DB for MCP tool results

Status: draft
Date: 2026-08-28
Implements: intent/001-scratch-db.md

## 1. Terminology

- **Session**: one Sluice process. v0 speaks stdio only, so one client connection
  equals one process equals one session equals one DuckDB instance. This is what
  makes "the DB dies with the session" true without any lifecycle code.
- **Downstream server**: an MCP server Sluice proxies to.
- **Envelope**: the generic table holding one row per proxied call.
- **Flat table**: the optional typed table materialized from a call's result.
- **Handle**: the object returned to the agent in place of a payload.
- **Preview**: the truncated rendering of the payload carried inside the handle.

## 2. Functional requirements

### Proxying

- **FR-1** Sluice starts a client session to each configured downstream server at
  startup and holds it for the process lifetime.
- **FR-2** Sluice's `tools/list` returns the union of downstream tools, each
  renamed to `<server>__<tool>`, plus Sluice's own `query`.
- **FR-3** Input schemas are passed through verbatim. The tool `description` is
  passed through verbatim with one appended sentence describing the handle
  contract. The `outputSchema`, if the downstream tool declares one, is
  **removed**. See §4.3 for why.
- **FR-4** A `tools/call` for `<server>__<tool>` is forwarded to that server as
  `<tool>` with arguments unmodified.
- **FR-5** If a downstream server is unreachable at startup, Sluice fails to
  start with a diagnostic on stderr. It does not start in a degraded state.
- **FR-6** v0 accepts a config with more than one server defined but refuses to
  start, with a message naming the limitation. Namespacing is built now; the
  fan-out is not.

### Recording

- **FR-7** Every proxied call writes exactly one envelope row, including calls
  that error and calls whose results pass through unmodified.
- **FR-8** Every proxied call that is eligible (§5.1) also produces a flat table.
  Eligibility includes selecting which channel the payload came from:
  `structuredContent` takes priority over text content (§5.1 step 3). The
  downstream `structuredContent` is preserved on the envelope row before Sluice
  overwrites the outgoing one with its handle.
- **FR-9** Materialization happens before the response is returned to the agent.
  A materialization failure must not fail the call: it is logged, the envelope
  row records the failure, and the agent receives an envelope-only handle.

### Returning

- **FR-10** Eligible results are replaced by a handle (§4).
- **FR-11** Results with `isError: true` are returned verbatim. No handle, no
  truncation. The envelope row still records them.
- **FR-12** Results containing any non-text content block (image, audio,
  embedded resource) are returned verbatim. The envelope records the block types
  and sizes, not the bytes.
- **FR-13** If the payload serializes to fewer bytes than the preview budget, the
  preview contains the entire payload. The agent gets a handle and the full data
  in one response, and no `query` round trip is needed to see it.

### Querying

- **FR-14** Sluice exposes one tool, `query(sql: str, max_rows: int = 100)`.
- **FR-15** `query` accepts exactly one statement and only a read statement
  (§6.1). Everything else is rejected before execution.
- **FR-16** `query` enforces a wall-clock timeout, a returned-row cap, and a
  returned-byte cap, and states in its output when any of them truncated the
  result.
- **FR-17** The session schema is introspectable through `query` itself. No
  second tool.

## 3. Storage model

### 3.1 The envelope

One table, created at session start:

```sql
CREATE TABLE sluice_calls (
    call_id      VARCHAR PRIMARY KEY,   -- uuid4
    seq          BIGINT,                -- monotonic within session, 1-based
    server       VARCHAR,
    tool         VARCHAR,               -- downstream name, not namespaced
    args         JSON,
    result       JSON,                  -- the payload actually materialized from (see 5.1)
    result_text  VARCHAR,               -- raw concatenated text content, always populated
    result_structured JSON,             -- downstream structuredContent verbatim, NULL if absent
    source_channel VARCHAR,             -- 'structured' | 'text' | 'none'
    byte_size    BIGINT,                -- of result_text plus serialized result_structured
    is_error     BOOLEAN,
    content_kinds VARCHAR[],            -- e.g. ['text'] or ['text','image']
    flat_table   VARCHAR,               -- NULL when no flat table was produced
    flat_reason  VARCHAR,               -- why not, when flat_table IS NULL
    source_path  VARCHAR,               -- JSONPath the flat rows came from, e.g. '$.items'
    started_at   TIMESTAMP,
    ended_at     TIMESTAMP,
    duration_ms  BIGINT
);
```

`result`, `result_text`, and `result_structured` are all stored deliberately.
`result` is the payload materialization actually ran against and is queryable
with DuckDB's JSON functions. `result_text` and `result_structured` are what the
downstream server actually sent, on each channel, and are the ground truth for
the recovery path in FR-14. Sluice overwrites the outgoing `structuredContent`
with its own handle (§4.2), so `result_structured` is the only place a
downstream tool's structured output survives. It must be written before the
handle is built.

### 3.2 Flat tables

Name: `<server>__<tool>__<seq3>`, for example `gh__list_issues__001`. `seq3` is
the zero-padded per-tool call counter, so a second call to the same tool becomes
`gh__list_issues__002`. Names are lowercased and non-alphanumerics become `_`.

Per-call tables, never appended to. This is what removes schema drift as a
failure mode entirely: a table's types are inferred once, from the complete set
of rows it will ever hold.

After each materialization, Sluice replaces the view `<server>__<tool>__latest`
to point at the newest table for that tool.

Every flat table carries two Sluice columns:

- `_row BIGINT`: 0-based ordinal preserving source array order.
- `_call_id VARCHAR`: joins back to `sluice_calls`.

If a source key collides with `_row` or `_call_id`, the source key is renamed to
`<key>__src` and the rename is noted in the handle's column list.

### 3.3 The schema view

```sql
CREATE VIEW sluice_schema AS ...  -- table_name, server, tool, seq, row_count, column_count, source_path, created_at
```

This satisfies FR-17. The agent can also use DuckDB's own `duckdb_tables()` and
`duckdb_columns()`.

## 4. The handle

### 4.1 Content channel

The tool result's `content` is replaced with a single `TextContent` block. This
is load-bearing: `content` is the only channel guaranteed to reach the model.
Format:

```
sluice: result materialized.
table: gh__list_issues__001   rows=412   from=$.items
columns: id BIGINT, number BIGINT, title VARCHAR, state VARCHAR, created_at VARCHAR,
         labels JSON*, user JSON*, _row BIGINT
         (* JSON: use json_extract and cast before arithmetic)
envelope: sluice_calls WHERE call_id = '0c3f8e1a-...'
preview (first 3 of 412 rows, 1.9 KB of 91.4 KB):
  {"id": 1841, "number": 212, "title": "worktree helper", "state": "open", ...}
  {"id": 1842, ...}
  {"id": 1843, ...}
Run SQL over this with the `query` tool.
```

When FR-13 applies (payload smaller than the preview budget) the preview line
reads `preview (complete, 412 B):` and the full payload follows. The agent is
told explicitly that nothing was withheld.

### 4.2 Structured channel

The same facts are mirrored into `structuredContent`:

```json
{
  "call_id": "0c3f8e1a-...",
  "table": "gh__list_issues__001",
  "envelope_table": "sluice_calls",
  "row_count": 412,
  "source_path": "$.items",
  "columns": [{"name": "id", "type": "BIGINT"}, ...],
  "byte_size": 91432,
  "preview_complete": false,
  "preview": "..."
}
```

### 4.3 Why `outputSchema` is removed

If a downstream tool declares an `outputSchema`, the MCP spec requires
`structuredContent` to conform to it. Sluice replaces the payload with a handle,
which by construction does not conform. Three options existed:

1. Override the downstream `outputSchema` with Sluice's handle schema. Honest
   about what is returned, but a client that read the tool's real schema
   somewhere else now sees a contradiction.
2. Keep the downstream schema and return non-conforming `structuredContent`.
   This is a spec violation and a strict client may reject the response.
3. Remove `outputSchema` from the mounted tool definition.

v0 takes option 3. A tool with no declared `outputSchema` may still return
`structuredContent`, unconstrained, which is exactly what Sluice needs. The cost
is that clients relying on downstream output schemas lose them, which is
acceptable for a proxy whose entire purpose is changing the shape of the output.

### 4.4 Description rewriting

Appended verbatim to each proxied description:

> Results from this tool are stored in a session database. You receive a preview
> plus a table name; use the `query` tool to run SQL over the full result.

## 5. Materialization

### 5.1 Eligibility

In order:

1. `isError` is true, or any content block is non-text: **passthrough**,
   envelope row only, no flat table.
2. Serialized payload exceeds `max_payload_bytes` (§7): **passthrough with a
   size note**, envelope row only, no parse, no flat table.
   `flat_reason = 'oversize'`. See §8 on why this ceiling exists.
3. Select the payload channel, in this order:
   a. `structuredContent` is present: **that is the payload.** It is already
      parsed and is the downstream tool's own declaration of its structured
      output. `source_channel = 'structured'`.
   b. Otherwise, exactly one text block parses as JSON: that block is the
      payload. `source_channel = 'text'`.
   c. Otherwise, the concatenation of all text blocks parses as JSON: the
      concatenation is the payload. `source_channel = 'text'`.
   d. Otherwise: **envelope only**, `flat_reason = 'not_json'`, and the agent
      gets a head-and-tail preview of the text.
4. Otherwise, attempt extraction (§5.2) against the selected payload.

Step 3a is load-bearing and was a blind spot in an earlier draft of this spec. A
tool that returns its data in `structuredContent` and a human-readable summary in
`content` is a normal and increasingly common shape. Materializing the text block
in that case would flatten the prose summary and silently discard the actual
data. The handle must report which channel it read, and it does, through
`source_channel`.

Step 3b before 3c is deliberate: two independently valid JSON text blocks
concatenate into invalid JSON, and the single-block case is far more common than
a genuine multi-block document.

### 5.2 Extraction: finding the rows

Real MCP results are rarely a bare array. The common shape is a text block
containing an envelope object like `{"items": [...], "next_cursor": "..."}`. So
extraction is its own step, not a special case of flattening.

Given the parsed payload:

- **A list of objects.** The list is the row set. `source_path = '$'`.
- **A list of scalars.** Materialize a one-column table `value`, plus `_row` and
  `_call_id`. `source_path = '$'`.
- **An object.** Scan top-level values for lists whose elements are at least 90%
  objects. If exactly one qualifies, use it. If several qualify, use the longest,
  and record the alternatives in the handle so the agent knows the choice was
  made and what was passed over. If none qualifies, treat the object itself as a
  single row and materialize a one-row table.
- **A scalar.** Envelope only. `flat_reason = 'scalar'`.
- **An empty list.** Create the table with zero rows if the columns can be
  determined, otherwise envelope only with `flat_reason = 'empty'`. The handle
  must say `rows=0` rather than silently returning nothing.

The chosen `source_path` is always reported in the handle. An agent that
disagrees with the choice can reach the untouched payload through
`sluice_calls.result`.

### 5.3 Depth-1 projection

For each row object, for each top-level key:

- Scalar value (string, number, boolean, null): kept as-is. DuckDB infers the
  column type.
- Object or array value: serialized to a JSON string. The column is typed `JSON`
  and the handle advertises it as such, so the agent knows to reach for
  `json_extract` rather than a dot path.

Nothing deeper than depth 1 becomes a column. This is a deliberate ceiling: the
column list is printed into the agent's context on every call, and a fully
inferred STRUCT schema for a deeply nested payload can be larger than the
payload preview it is meant to replace.

**Column explosion.** If the union of keys exceeds `max_columns` (default 64),
keep the 64 with the highest presence rate across rows and collect the remainder
into an `_extra JSON` column. The handle states that this happened and how many
keys went into `_extra`. Below the cap, a rare key simply becomes a mostly-null
column, which is informative and costs nothing.

**Column types** are Sluice's own, not DuckDB's inference. See §5.5 for the
rules and for the three measured DuckDB behaviors that motivated them.

**Heterogeneity** is therefore not a binary pass or fail. Rows with different key
sets produce a union schema with NULLs, which is a correct and queryable
representation of the data that actually arrived.

### 5.4 Loading into DuckDB

**No temp files, and no `read_json`.** This is forced by §6.1, not chosen.
`SET enable_external_access = false` is a database-global setting, and it blocks
DuckDB's own file readers, Sluice's included. A file-based load and the security
posture cannot coexist:

- `read_json` over a temp file under the lockdown fails with
  `PermissionException: file system operations are disabled by configuration`.
  Verified, DuckDB 1.5.5.
- `SET allowed_directories = [...]` does not confine access. Under it,
  `read_csv('/etc/hosts')`, `COPY ... TO '/tmp/...'`, `ATTACH`, `INSTALL httpfs`,
  and `glob('/etc/*')` all still succeeded. Verified, DuckDB 1.5.5.
- The setting is database-global, so a writer connection with access enabled and
  a query connection with it disabled is not possible against one database.
  Verified, DuckDB 1.5.5.

So materialization builds the table directly:

1. Infer a column type per column from the **complete** projected row set (§5.5).
2. `CREATE TABLE <name> (<col> <TYPE>, ..., _row BIGINT, _call_id VARCHAR)`.
3. `executemany` the projected rows.

Verified to work under the full lockdown, with JSON columns still queryable via
`json_extract`.

Three consequences worth stating plainly:

- **Sluice owns type inference now.** The original scope assumed DuckDB's JSON
  inference would do most of this work. It cannot, given the lockdown. This is
  the largest single change the M0 spike forced.
- The NDJSON buffer leaves the pipeline, so peak memory drops and there is no
  temp file to leak or clean up. The §5.1 `max_payload_bytes` figure was derived
  from a measurement of the file-based pipeline and must be re-measured.
- Inference becomes a thing Sluice is responsible for getting right, and
  therefore a thing that must be tested directly rather than trusted.

If table creation or insertion raises, catch it, record
`flat_reason = 'load_failed: ...'` on the envelope row, and return an
envelope-only handle. FR-9: a materialization failure never fails the tool call.

### 5.5 Type inference rules

Per column, over every value present in the complete row set, ignoring nulls:

| Values observed | Column type |
|---|---|
| all bool | `BOOLEAN` |
| all int, all within int64 | `BIGINT` |
| all int, any outside int64 but within int128 | `HUGEINT` |
| all int, any outside int128 | `VARCHAR`, flagged lossy in the handle |
| all numeric, at least one float | `DOUBLE` |
| all strings | `VARCHAR` |
| any object or array | `JSON` |
| mixed scalar types | `VARCHAR`, flagged mixed in the handle |
| all null, or column absent everywhere | `VARCHAR` |

Three rules that exist because of measured DuckDB behavior:

1. **Never infer `TIMESTAMP` from a string.** DuckDB infers ISO-8601-shaped
   strings as `TIMESTAMP` and returns them naive, silently dropping the `Z`
   offset. Version strings and opaque ids that happen to look like dates get
   swept up the same way. v0 keeps them `VARCHAR`; the agent can `CAST` in SQL,
   where the conversion is visible.
2. **Mixed scalar columns become `VARCHAR`, never `JSON`.** DuckDB's own
   inference assigns `JSON` to a column that is integer for 300 rows and a string
   on row 301. On a `JSON` column, `sum()` and `avg()` do not exist (binder
   error, which is loud and fine) but **`median()` succeeds and returns a
   lexicographic result**: over the integers 0 to 299 plus one string, it
   returned `'232'`. A plausible-looking number that is silently wrong is the
   exact failure this project exists to prevent. `VARCHAR` makes the same query
   fail loudly instead.
3. **`HUGEINT` is real and exact** through int128, so integers past int64 do not
   have to widen to `DOUBLE` and lose precision. DuckDB assigns `HUGEINT` up to
   unsigned-64 max on its own; Sluice does it deliberately and further.

`JSON` columns are aggregation traps even when correctly typed, so the handle
marks them (§4.1) and the column list states that they need `json_extract` and a
cast before any arithmetic.

## 6. The `query` tool

### 6.1 Read-only enforcement

"Read-only" cannot be implemented as "the string starts with SELECT", because
`SELECT * FROM read_csv('/etc/passwd')` starts with SELECT. Enforcement is two
independent layers:

**Layer 1, statement gate.** Use DuckDB's own parser through
`connection.extract_statements(sql)`. Require exactly one statement, and require
its type to be `StatementType.SELECT`. A `WITH ... SELECT` parses as SELECT and
is allowed. Anything else (INSERT, UPDATE, DELETE, CREATE, DROP, ATTACH, COPY,
PRAGMA, CALL, SET, EXPORT, multiple statements) is rejected with a message naming
the statement type. Using the engine's parser rather than a regex means the gate
cannot be defeated by comments, casing, or whitespace tricks.

**Layer 2, engine lockdown.** At session start, before any user SQL runs:

```
SET enable_external_access = false;      -- kills read_csv/read_parquet/httpfs/ATTACH to files
SET autoinstall_known_extensions = false;
SET autoload_known_extensions = false;
SET allow_community_extensions = false;
SET temp_directory = '<session temp dir>';
SET max_memory = '<configured, default 1GB>';
SET lock_configuration = true;           -- must be last; no SET can undo the above
```

Layer 1 alone would still permit filesystem reads inside a SELECT. Layer 2 alone
would still permit `CREATE TABLE`, and it does not block `PRAGMA` (verified:
`PRAGMA database_list` succeeds under the full lockdown, and is stopped only by
the layer-1 statement gate). Both layers are required.

Verified blocked under this sequence, DuckDB 1.5.5: `read_csv`, `read_json`,
`read_parquet`, `glob`, `ATTACH`, `COPY ... TO`, `INSTALL`, `LOAD`, and any
later `SET` of a locked option.

**This lockdown is why §5.4 cannot use `read_json`.** The setting is
database-global and applies to Sluice's own SQL as much as the agent's. Do not
"fix" a materialization failure by relaxing it.

### 6.2 Timeout

DuckDB has no statement timeout setting. The mechanism is: execute on a dedicated
cursor inside a worker thread, and have the event loop call `interrupt()` on the
connection when the deadline passes. Default 10 seconds, configurable.

**The watchdog must interrupt the exact `DuckDBPyConnection` object the worker is
executing on.** Measured, DuckDB 1.5.5: `interrupt()` on a *parent* connection
does not stop work running on a cursor derived from it (the query ran to
completion, 5.2s). `interrupt()` on the object actually executing the query
stopped it in 0.2s. Interrupt scope is the connection object, not the
parent-and-children family.

Isolation is therefore confirmed in both directions: a concurrent write on a
sibling cursor committed intact, and all connections stayed usable. The same
held for separately opened connections to one named in-memory database.

An implementation whose watchdog interrupts the parent silently does nothing,
and the timeout appears to work only because short queries finish anyway. Test
it with a query that genuinely runs long.

### 6.3 Result shaping

- Execute the user SQL **unmodified** and pull `max_rows + 1` rows with
  `fetchmany`. The extra row is how truncation is detected.

  Do not wrap the SQL as `SELECT * FROM (<sql>) LIMIT n+1`. Wrapping is a source
  of failures on statements that are individually valid: a trailing semicolon,
  a result set with duplicate column names, and other legal SELECT forms break
  inside a subquery even though they passed the §6.1 gate. Rejecting SQL the
  agent was told is allowed is worse than the problem the wrapper solves.
  `fetchmany` also avoids materializing the full result set, since DuckDB
  streams.
- `max_rows` defaults to 100 and is capped at 1000.
- Render as a markdown table. Values that are JSON or long strings are truncated
  per cell at `max_cell_bytes` (default 512) with a marker. All byte-budget
  truncation, here and in the preview, must cut on a character boundary. Slicing
  a UTF-8 payload at a byte offset produces invalid text and can corrupt the
  handle the agent depends on.
- Enforce a total byte cap (default 64 KB) on the rendered output. If exceeded,
  stop emitting rows and say how many were omitted.
- Every truncation is stated in the output. A silently truncated result is a
  correctness bug in a tool whose entire selling point is determinism.

### 6.4 Recovering a full payload

There is no `fetch` tool. The agent recovers the untouched payload with:

```sql
SELECT result_text FROM sluice_calls WHERE call_id = '...'
```

This is subject to the byte cap in §6.3, which is the intended behavior: pulling
a 90 KB payload into context should require asking for it explicitly and should
still be bounded.

## 7. Configuration

`sluice.toml`, discovered by `--config`, then `$SLUICE_CONFIG`, then
`./sluice.toml`.

```toml
[servers.gh]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }

[servers.remote]
url = "https://example.com/mcp"        # streamable http; mutually exclusive with command

[limits]
max_payload_bytes = 33554432   # 32 MB; above this, pass through without parsing
preview_bytes = 2048
preview_rows = 3
max_columns = 64
query_timeout_seconds = 10
query_max_rows = 100
query_max_bytes = 65536
max_cell_bytes = 512
duckdb_max_memory = "1GB"
```

`${VAR}` in `env` values expands from the Sluice process environment. Secrets are
not stored in the config file.

## 8. Failure behavior

| Situation | Behavior |
|---|---|
| Downstream unreachable at startup | Fail to start, diagnostic on stderr |
| Downstream call raises | Envelope row with `is_error`, error returned verbatim |
| Result is not JSON on any channel | Envelope-only handle, head-and-tail text preview |
| Payload exceeds `max_payload_bytes` | Passthrough with a size note, no parse, envelope row records the size |
| Flattening, inference, or insert fails | Envelope-only handle, `flat_reason` records the cause, call succeeds |
| DuckDB write fails | Log to stderr, return the original result unmodified, do not fail the call |
| `query` rejects the SQL | Tool error naming the reason, never a silent empty result |
| `query` times out | Tool error stating the timeout, with the elapsed budget |

The invariant across this table: **Sluice never turns a working tool call into a
failed one.** Every degradation path ends with the agent holding something usable.

The invariant has exactly two boundaries, and both are named here rather than
left implicit:

1. **Process death.** An out-of-memory kill during materialization takes the
   whole session, not just the call, and no `except` clause can catch it. The
   invariant is therefore conditional on staying inside the memory budget, which
   is what `max_payload_bytes` (§5.1 step 2) exists to guarantee. The file-free
   load in §5.4 removes one copy from the pipeline, so the ceiling needs
   re-deriving against the new shape. A recovery path that assumes the process survives is not a
   recovery path.
2. **Connection-wide interrupt.** A timed-out `query` must not abort a concurrent
   write. See §6.2.

## 9. Concurrency

MCP clients may issue concurrent tool calls. DuckDB Python connections are not
safe for concurrent use from multiple threads on the same cursor. v0:

- One writer connection per session, one `cursor()` per operation.
- `query` executes on a **separate connection** to the same in-memory database,
  so the §6.2 timeout interrupt cannot reach a materialization write. If M0
  proves interrupts are already cursor-scoped, this can collapse back to one
  connection; do not assume it in advance.
- Writes serialized behind an `asyncio.Lock`.
- All DuckDB calls, which are blocking, dispatched through `asyncio.to_thread`
  so the MCP event loop is never blocked.

## 10. Contradictions and decisions not actually made

Flagged as requested, with the resolution this spec takes.

1. **"The table name" is ambiguous when both an envelope row and a flat table
   exist.** Resolved: the handle names the flat table when there is one and
   always names the envelope row as well. Both are always reachable.
2. **"Opportunistic flattening" implied a binary homogeneity test that the data
   will not support.** Resolved as a coverage model (§5.3), so heterogeneity
   degrades into sparse columns instead of failing to a table-less result.
3. **"Read-only SQL" was underspecified** and the obvious implementation is
   insecure. Resolved in §6.1 as two layers.
4. **"Statement timeout" implies a setting that DuckDB does not have.** Resolved
   in §6.2 as an interrupt from a watchdog.
5. **The correctness criterion conflates a property test and a demo.** The
   aggregate-equality property is deterministic and gates CI. The
   "agent gets the median wrong without Sluice" demonstration is a model eval,
   is non-deterministic, and must not gate CI.
6. **"One downstream server" and "namespacing to avoid collisions" are in
   tension.** With one server there are no collisions. Resolved: build the
   namespacing now, config accepts several, v0 refuses to start with more than
   one (FR-6).
7. **Always-intercept was chosen against the recommendation to size-gate.** The
   cost is a `query` round trip on small results. FR-13 answers this by making
   the preview complete when the payload fits in the budget, so the round trip is
   never forced for data the agent could simply have been given.
8. **`structuredContent` was treated as an output channel only.** The original
   draft materialized parsed text and never considered that the downstream tool's
   real data may arrive in `structuredContent`, with `content` holding only a
   prose summary. Resolved in §5.1 step 3 as an explicit channel priority, with
   `source_channel` reported on the envelope and in the handle. This was an
   architecture-level gap, not a refinement.
9. **The "never fails a working call" invariant was stated without boundaries**
   and cannot survive an OOM kill, which ends the process rather than the call.
   Resolved in §8 by naming both boundaries and adding `max_payload_bytes`.
10. **The §6.1 lockdown and the §5.4 `read_json` load were mutually exclusive**,
    and both were written into the same spec. `enable_external_access` is
    database-global and blocks Sluice's own file readers. Neither
    `allowed_directories` nor a writer/query connection split works around it.
    Resolved by making materialization file-free (§5.4) and giving Sluice its own
    inference rules (§5.5). This invalidated the premise that DuckDB's JSON
    inference would do most of the work.
11. **Session scope was undefined for non-stdio transports.** Resolved by
   restricting v0 to stdio, which makes process lifetime and session lifetime the
   same thing. An HTTP transport would need a session-id-keyed map of databases
   and an eviction policy, and that is not v0.

## 11. Open questions

1. **Python floor 3.14 or 3.15.** 3.15 releases 2026-10-01. Gating factor is
   DuckDB cp315 wheel availability, since DuckDB is a compiled extension and a
   source build is not an acceptable install path. Not a decision for this spec.
2. **Repeated calls to the same tool.** v0 gives each its own table plus a
   `__latest` view. Whether a union view across calls is wanted is unknown until
   observed.
3. **Pagination.** A downstream tool that pages produces N tables that the agent
   must UNION by hand. Detecting a cursor field and offering a combined view is a
   plausible v1 feature and is not in v0.
4. **Whether `_extra` and JSON columns are actually used** by a model in practice,
   or whether the agent silently ignores anything that is not a scalar column.
   This is an observation to make, not a thing to design around yet.
