# Spec 001: Scratch DB for MCP tool results

Status: draft (revision 3, post-M0 and post-review)
Date: 2026-08-28
Implements: intent/001-scratch-db.md
Evidence: plan/001-notes-m0.md

Revision 3 incorporates the M0 spike results and a design review. Sections that
changed materially: §1, §2, §3, §4, §5.2, §5.3, §5.5, §5.6, §6, §7, §9, §10, §12.

**Pinned protocol revision: MCP `2026-07-28`. Pinned SDK: `mcp` 2.1.1. Pinned
engine: DuckDB 1.5.5.** Every normative claim below is against those versions.
Behavior differs across revisions; do not generalize.

## 1. Terminology

- **Process**: one Sluice process. v0 speaks stdio only.
- **Scope**: the isolation boundary for materialized tables. A client may reuse
  one stdio process across several conversations, so process lifetime and
  conversation lifetime are not the same thing. See §12.
- **Downstream server**: an MCP server Sluice proxies to.
- **Envelope**: the generic table holding one row per proxied call.
- **Flat table**: a typed table materialized from a call's result.
- **Handle**: the object returned to the agent in place of a payload.
- **Preview**: the truncated rendering of the payload carried inside the handle.

## 2. Functional requirements

### Proxying

- **FR-1** Sluice starts a client session to each configured downstream server at
  startup and holds it for the process lifetime.
- **FR-2** Sluice's `tools/list` returns the union of downstream tools, each
  renamed per §3.2, plus Sluice's own `query`. **Downstream listing is
  paginated**: `ClientSession.list_tools()` returns a single page and does not
  follow `next_cursor` (verified, SDK 2.1.1). Sluice loops until `next_cursor`
  is `None`, treating `""` as a valid cursor rather than a terminator. A tool
  that appears only on page 2 must still be mounted.
- **FR-3** Sluice **clones the entire downstream tool object** and mutates only
  three fields: `name` (§3.2), `description` (§4.4), and `outputSchema`
  (removed, §4.3). Every other field is preserved verbatim, including `title`,
  `icons`, `annotations`, `_meta`, and any field this spec does not enumerate.
  Reconstructing a tool field by field is prohibited: dropping
  `annotations.destructiveHint` would strip the signal a client uses to ask the
  user for confirmation, turning a safety feature off silently.
- **FR-4** A call for a mounted tool is forwarded to its server under the
  original name with arguments unmodified.
- **FR-5** Multi-round-trip calls are **relayed end to end**. `InputRequiredResult`
  from downstream is returned upstream unchanged; `input_responses` and the
  opaque `request_state` from upstream are forwarded downstream unchanged.
  Materialization happens only on a final `CallToolResult`. Sluice must not
  answer an elicitation or sampling request itself: those are addressed to the
  real client and its human. See §11.
- **FR-6** If a downstream server is unreachable at startup, Sluice fails to
  start with a diagnostic on stderr. It does not start in a degraded state.
- **FR-7** v0 accepts a config with more than one server defined but refuses to
  start, naming the limitation. Namespacing is built now; fan-out is not.

### Recording

- **FR-8** Every proxied call **whose envelope write succeeds** has exactly one
  envelope row, including calls that error and calls that pass through. If the
  envelope write itself fails, there is no row to write and no fallback store;
  Sluice logs to stderr and returns the downstream result unmodified. FR-8 is
  conditional on that write, which §8 previously contradicted.
- **FR-9** Every proxied call that is eligible (§5.1) also produces one or more
  flat tables. Eligibility includes selecting which channel the payload came
  from: `structuredContent` takes priority over text content (§5.1). The
  downstream `structuredContent` is preserved on the envelope row before Sluice
  overwrites the outgoing one with its handle.
- **FR-10** Materialization happens before the response is returned. A
  materialization failure must not fail the call: it is logged, the envelope row
  records the failure, and the agent receives an envelope-only handle.

### Returning

- **FR-11** Eligible results are replaced by a handle (§4).
- **FR-12** Results with `isError: true` are returned verbatim. No handle, no
  truncation. Other failure classes are **not** verbatim and cannot be; see §8.
- **FR-13** Results containing any non-text content block (image, audio,
  embedded resource) are returned verbatim. The envelope records the block types
  and sizes, not the bytes.
- **FR-14** If the payload serializes to fewer bytes than the preview budget, the
  preview contains the entire payload and says so.

### Querying

- **FR-15** Sluice exposes one tool, `query(sql: str, max_rows: int = 100)`.
- **FR-16** `query` accepts exactly one statement and only a read statement
  (§6.1). Everything else is rejected before execution.
- **FR-17** `query` enforces a wall-clock timeout, a returned-row cap, and a
  returned-byte cap, and states when any of them truncated the result.
- **FR-18** **Catalog enumeration is blocked** (§6.1, §12). The agent's own
  handles are the index of what exists. This reverses revision 1's FR-17 and is
  the direct cost of the scope isolation decision.

## 3. Storage model

### 3.1 The envelope

```sql
CREATE TABLE sluice_calls (
    call_id      VARCHAR PRIMARY KEY,   -- uuid4
    scope_id     VARCHAR,               -- §12
    seq          BIGINT,                -- monotonic within process, 1-based
    server       VARCHAR,
    tool         VARCHAR,               -- downstream name, not namespaced
    args         JSON,
    result       JSON,                  -- the payload materialization ran against; NULL when oversize
    result_text  VARCHAR,               -- concatenated text content; NULL when oversize
    result_blocks JSON,                 -- ordered [{type, text|meta}] preserving block boundaries
    result_structured JSON,             -- downstream structuredContent verbatim; NULL if absent or oversize
    source_channel VARCHAR,             -- 'structured' | 'text' | 'none'
    channel_conflict BOOLEAN,           -- both channels parsed and disagreed (§5.1)
    byte_size    BIGINT,                -- selected payload, serialized (§5.1)
    wire_bytes   BIGINT,                -- total result size on the wire
    is_error     BOOLEAN,
    failure_class VARCHAR,              -- §8; NULL on success
    content_kinds VARCHAR[],
    flat_tables  VARCHAR[],             -- may hold several (§5.2); empty when none
    flat_reason  VARCHAR,               -- why empty
    source_paths VARCHAR[],             -- parallel to flat_tables
    started_at   TIMESTAMP,
    ended_at     TIMESTAMP,
    duration_ms  BIGINT
);
```

`result_blocks` exists because `result_text` alone is lossy: two text blocks
concatenate irreversibly, and §6.4's recovery path must be able to return what
the server actually sent.

On an oversize result (§5.1), the payload columns are `NULL` and only the size
columns are populated. Revision 2 required both "no parse" and a populated
`result`, which was self-contradictory.

### 3.2 Naming

One injective algorithm, used for both mounted tool names and table names.
Sanitizing alone is not injective: the distinct MCP tool names `a-b` and `a_b`,
or `Foo` and `foo`, collapse to the same string, and MCP tool names are
case-sensitive and may contain hyphens and dots.

```
slug(s)   = lowercase, non-alphanumerics -> "_", truncated to 40 chars
tag(s)    = first 6 chars of blake2b(s.encode()) hexdigest   # of the UNSANITIZED name
mounted   = f"{slug(server)}__{slug(tool)}__{tag(server + '\x00' + tool)}"
table     = f"{mounted}__{scope_tag}__{seq:04d}"
```

- The hash tag makes the mapping injective. `a-b` and `a_b` get different tags.
- Mounted names are validated against the 128-character tool-name limit at
  startup; a name that cannot fit fails startup loudly rather than silently
  colliding.
- `scope_tag` is 8 unguessable characters (§12). It is what stops a stale handle
  from a previous process resolving to live data holding different contents.
  Without it, sequence numbers restart at 1 on every process start and a resumed
  conversation's handle can name a table that exists and is wrong.
- `seq` is zero-padded to 4 and simply widens past 9999. Padding governs lexical
  sort only.
- **Every identifier is quoted in generated SQL.** No exceptions, including
  column names.

Internal column names are allocated recursively: if a source key sanitizes to
`_row`, `_call_id`, or `_extra`, it becomes `<key>__src`; if `<key>__src` is also
taken, `<key>__src2`, and so on until free. The handle publishes the original-to-
stored mapping for every renamed column. Two source keys that sanitize
identically are disambiguated the same way.

Sluice replaces the view `<mounted>__latest` after each materialization. **Latest
means highest `seq`, not last written.** Calls may complete out of order, so a
call that started first and finished second must not claim the view.

### 3.3 No schema view

Revision 1 defined `sluice_schema` for discovery. It is removed. Enumeration is
what §12's isolation has to block, so a view whose purpose is enumeration cannot
exist. See FR-18.

## 4. The handle

### 4.1 Content channel

`content` is replaced with a single `TextContent` block. Format:

```
sluice: result materialized.  channel=structured  scope=k7d92m
table: gh__list_issues__3f9a1c__k7d92m__0001   rows=412   from=$.items
columns: id BIGINT, number BIGINT, title VARCHAR, state VARCHAR,
         created_at VARCHAR, labels JSON*, user JSON*, _row BIGINT
         (* JSON: use json_extract and cast before arithmetic)
also materialized: gh__list_issues__3f9a1c__k7d92m__0002  rows=100  from=$.facets
envelope: call_id='0c3f8e1a-...'
preview (first 3 of 412 rows, 1.9 KB of 91.4 KB):
  {"id": 1841, ...}
Run SQL over this with the `query` tool.
```

Mandatory fields, all of which revision 2 promised somewhere in prose while
omitting them from the rendered handle: `channel`, `scope`, every materialized
table with its `source_path`, and any renamed-column mapping. When
`channel_conflict` is true the handle says so explicitly and names both
candidates.

When FR-14 applies the preview line reads `preview (complete, 412 B):`.

### 4.2 Structured channel

```json
{
  "call_id": "0c3f8e1a-...",
  "scope_id": "k7d92m",
  "source_channel": "structured",
  "channel_conflict": false,
  "tables": [
    {"name": "...__0001", "source_path": "$.items", "row_count": 412,
     "columns": [{"name": "id", "type": "BIGINT", "exact": true}],
     "renamed": {"_row": "_row__src"}}
  ],
  "byte_size": 91432,
  "preview_complete": false,
  "preview": "..."
}
```

`exact` is the §5.6 flag: false means the column falls outside the domain where
the correctness guarantee holds.

### 4.3 Why `outputSchema` is removed

Under MCP `2026-07-28`, a tool that declares an `outputSchema` MUST return
conforming structured results, and clients SHOULD validate. Sluice replaces the
payload with a handle, which does not conform. A tool with no declared
`outputSchema` may still return `structuredContent`, unconstrained. So removal is
the only option that is both honest and legal.

Note the asymmetry, corrected from revision 2: schema conformance is **normative**;
the claim that `content` is what reaches the model and `_meta` is not is
**client convention**, not protocol law. The decision to put the handle in
`content` stands as a portability choice, not as a guarantee MCP provides.

### 4.4 Description rewriting

Appended verbatim to each proxied description:

> Results from this tool are stored in a session database. You receive a preview
> plus a table name; use the `query` tool to run SQL over the full result.

## 5. Materialization

### 5.1 Eligibility and channel selection

1. `isError` is true, or any content block is non-text: **passthrough**, envelope
   row only.
2. Selected payload exceeds `max_payload_bytes` (§7): **passthrough with a size
   note**, envelope row with payload columns `NULL`.

   Byte accounting is explicit because two numbers differ. `wire_bytes` is the
   whole result. `byte_size` is the serialized selected payload, and for a
   `structuredContent` payload the SDK has **already decoded it** before Sluice
   can measure it, so this check cannot prevent that decode. It bounds what
   Sluice does next, not what the SDK already did. Do not describe it as an OOM
   guarantee for structured results.
3. Select the payload channel:
   a. `structuredContent` present: that is the payload. `source_channel='structured'`.
   b. Otherwise exactly one text block parses as JSON: that block.
   c. Otherwise the concatenation of text blocks parses as JSON: the concatenation.
   d. Otherwise **envelope only**, `flat_reason='not_json'`, head-and-tail preview.
4. **Conflict detection.** If both `structuredContent` and a text channel parse
   and are not equal, set `channel_conflict`, materialize the structured channel,
   and say so in the handle. Two conflicting candidates for "the raw source data"
   silently resolved is a correctness hazard, and the criterion in §5.6 is
   meaningless if the source is ambiguous.
5. Otherwise extract (§5.2).

Step 3a was a blind spot in revision 1. A tool returning data in
`structuredContent` and a prose summary in `content` would have had the summary
flattened and the data discarded.

### 5.2 Extraction

Given the parsed payload:

- **A list, every element an object.** The list is the row set, `source_path='$'`.
- **A list of scalars.** One-column table `value`.
- **A list mixing objects and non-objects.** Not eligible. `flat_reason='mixed_elements'`.
  Revision 2's 90%-objects threshold left the remaining 10% undefined, and
  implementers would variously drop, wrap, or fail on them, changing `count(*)`.
  100% or nothing.
- **An object.** Every top-level value that is a list of objects is a candidate.
  **Materialize each candidate as its own table**, named by §3.2 with distinct
  sequence numbers, all listed in the handle with their paths. Sluice does not
  choose. Revision 2 picked the longest, which on
  `{"rows": [...20], "facets": [...100]}` silently aggregates facet buckets when
  the agent asked about rows, and reporting the path afterward does not undo a
  wrong answer already given.
  If no value qualifies, the object itself is one row.
- **A scalar.** Envelope only, `flat_reason='scalar'`.
- **An empty list.** Zero-row table if columns are determinable, else envelope
  only with `flat_reason='empty'`. The handle says `rows=0` rather than nothing.

### 5.3 Depth-1 projection

For each row object, for each top-level key: scalar values are kept; object and
array values are serialized to JSON strings and typed `JSON`. Nothing deeper than
depth 1 becomes a column, because the column list is printed into the agent's
context on every call.

Column types are Sluice's own (§5.5), not DuckDB's inference. §5.4 explains why
that is forced.

**Missing keys and JSON `null` both become SQL `NULL`,** and the flat table cannot
distinguish them. This is a deliberate, lossy normalization, stated here because
it changes results: presence-sensitive `GROUP BY` and `COUNT(DISTINCT)` behave
differently from a Python reference that keeps a missing-key sentinel. §5.6
defines the reference against this normalization rather than against the raw
JSON, which is the only way the criterion can hold.

**Column explosion.** Above `max_columns` (default 64), keep the 64 with the
highest presence rate; ties break by first-appearance order in the payload,
which is deterministic. The remainder goes to `_extra JSON` and the handle says
how many keys went there. Revision 2 left ties undefined, so a payload with 200
equally-present keys had no defined column set.

### 5.4 Loading into DuckDB

**No temp files, and no `read_json`.** This is forced by §6.1, not chosen.
`SET enable_external_access = false` is database-global and blocks DuckDB's own
file readers, Sluice's included. Verified, DuckDB 1.5.5:

- `read_json` over a temp file under the lockdown fails with
  `PermissionException: file system operations are disabled by configuration`.
- `SET allowed_directories = [...]` does not confine access: under it,
  `read_csv('/etc/hosts')`, `COPY ... TO '/tmp/...'`, `ATTACH`, `INSTALL httpfs`,
  and `glob('/etc/*')` all still succeeded.
- The setting is database-global, so a writer connection with access plus a query
  connection without it is not possible against one database.

So materialization builds the table directly: infer a column type per column from
the complete projected row set (§5.5), `CREATE TABLE` with explicit types and
quoted identifiers, then `executemany`. Verified to work under the full lockdown,
with `JSON` columns still queryable through `json_extract`.

**Sluice owns type inference now.** The original scope assumed DuckDB's JSON
inference would do the work. It cannot. This is the largest single change the M0
spike forced, and it moves every inference bug from DuckDB's ledger to ours.

If creation or insertion raises, catch it, record `flat_reason='load_failed: ...'`,
and return an envelope-only handle (FR-10).

### 5.5 Type inference rules

Per column, over every non-null value in the complete row set:

| Values observed | Column type | `exact` |
|---|---|---|
| all bool | `BOOLEAN` | true |
| all int, within int64 | `BIGINT` | true |
| all int, within int128 | `HUGEINT` | true |
| all int, beyond int128 | `VARCHAR` | false |
| all numeric, at least one float, **every int within ±2^53** | `DOUBLE` | true |
| all numeric, at least one float, some int beyond ±2^53 | `VARCHAR` | false |
| any non-finite float (`inf`, `nan`) present | `DOUBLE` | false |
| all strings | `VARCHAR` | true |
| any object or array | `JSON` | false |
| mixed scalar types | `VARCHAR` | false |
| all null or absent | `VARCHAR` | true |

Rules that exist because of measured behavior:

1. **Never infer `TIMESTAMP` from a string.** DuckDB infers ISO-8601-shaped
   strings as `TIMESTAMP` and returns them naive, dropping the `Z`. Version
   strings and opaque ids that look like dates get swept up the same way. v0
   keeps them `VARCHAR`; the agent can `CAST`, where the conversion is visible.
2. **Mixed scalar columns become `VARCHAR`, never `JSON`.** On a `JSON` column
   `sum()` and `avg()` raise a binder error, which is loud and fine, but
   `median()` succeeds and returns a lexicographic result: over integers 0 to 299
   plus one string it returned `'232'`. A number-shaped lie is the exact failure
   this project exists to prevent.
3. **The ±2^53 rule.** `[9007199254740993, 0.5]` typed `DOUBLE` returns
   `max = 9007199254740992.0`, against a true maximum of `9007199254740993`.
   Measured. A mixed int-and-float column containing integers outside binary64's
   exact range is marked non-exact.

### 5.6 The correctness contract

The project's criterion is that an aggregate computed via `query` equals the same
aggregate computed over the raw source data. Revision 2 stated that without a
domain, and it is false outside one.

**The reference.** Equality is defined against the §5.3 normalization (missing
key and JSON `null` are both SQL `NULL`), not against raw JSON. The Python
reference skips `None` explicitly, exactly as SQL aggregates skip `NULL`, and
`COUNT(DISTINCT)` compares against `len({v for v in col if v is not None})`.

**The safe domain.** Exactness is claimed only for columns marked `exact` in
§5.5, with no non-finite values present.

**Exact equality**, inside the domain, verified on DuckDB 1.5.5:
`count(*)`, `count(col)`, `min`, `max`, `count(DISTINCT)`, `GROUP BY` counts,
integer `sum`, and `median` on `BIGINT` and `DOUBLE` at even and odd row counts.

**Within tolerance**: `avg` and float `sum`, asserted with `math.isclose` at a
relative tolerance of `1e-9` **and** an absolute tolerance of `1e-12`. Both are
required: relative tolerance alone does not survive cancellation. Measured,
`avg([-1e308, 1.0, 2.0, 1e308])` is `0.0` in DuckDB and `0.75` in
`statistics.fmean`, an infinite relative error.

**Outside the domain there is no guarantee**, and the handle says so per column
via `exact: false`. Measured counterexamples, all outside it:
`median([0, 2^63-2, 2^63-1])` gives `9.223372036854776e+18` against an exact
`9223372036854775806`; `median([1e308, 1e308])` gives `1e308` while
`statistics.median` overflows to `inf`.

## 6. The `query` tool

### 6.1 Read-only enforcement

Three layers.

**Layer 1, statement gate.** `connection.extract_statements(sql)`. Require
exactly one statement of type `StatementType.SELECT`. `WITH ... SELECT` parses as
SELECT and is allowed. Verified: trailing semicolons, a semicolon inside a
comment, and CTEs all parse to one statement with no preprocessing, and
`SELECT 1; SELECT 2` parses to two and is rejected. Using the engine's parser
means the gate cannot be defeated by comments, casing, or whitespace.

**Layer 2, engine lockdown.** At session start, before any user SQL:

```
SET enable_external_access = false;
SET autoinstall_known_extensions = false;
SET autoload_known_extensions = false;
SET allow_community_extensions = false;
SET max_memory = '<configured>';
SET lock_configuration = true;           -- last
```

Verified blocked: `read_csv`, `read_json`, `read_parquet`, `glob`, `ATTACH`,
`COPY ... TO`, `INSTALL`, `LOAD`, and any later `SET` of a locked option.
Verified **not** blocked: `PRAGMA database_list`, which only layer 1 stops.

**This lockdown is why §5.4 cannot use `read_json`.** Do not "fix" a
materialization failure by relaxing it.

**Layer 3, catalog denylist.** Reject SQL referencing `duckdb_tables`,
`duckdb_columns`, `duckdb_views`, `information_schema`, `pg_catalog`, or
`sqlite_master`. This enforces FR-18 and §12. It is a denylist and therefore
weaker than the other two layers; §12 states the residual risk.

### 6.2 Timeout

DuckDB has no statement timeout. Execute on a **dedicated connection per
in-flight query**, and have the event loop call `interrupt()` on that exact
connection object when the deadline passes. Default 10 seconds.

Measured, DuckDB 1.5.5: `interrupt()` on a *parent* connection does not stop work
on a cursor derived from it (the query ran to completion, 5.2s). `interrupt()` on
the object actually executing stopped it in 0.2s. Scope is the connection object.
A watchdog that interrupts the parent silently does nothing and appears to work
only because short queries finish anyway.

One connection per in-flight query, not one shared query connection: two
concurrent queries on one connection would let either one's timeout abort the
other.

### 6.3 Result shaping

- Execute the user SQL **unmodified** and pull `max_rows + 1` rows with
  `fetchmany`. Do not wrap as `SELECT * FROM (<sql>) LIMIT n+1`: trailing
  semicolons and duplicate column names break inside a subquery even though they
  passed layer 1, and rejecting SQL the agent was told is legal is worse than the
  cap it solves.
- `max_rows` defaults to 100, capped at 1000.
- The extra row proves **at least one** row was omitted. It does not give a
  count, and the output must say "additional rows exist" rather than a number
  Sluice cannot know without a second execution.
- Render as a markdown table. Escaping is defined, not left to the implementer:
  `|` becomes `\|`, newlines become `\n`, `NULL` renders as the literal `NULL`
  and an empty string as `''` so the two are distinguishable.
- Per-cell truncation at `max_cell_bytes` (default 512), total output at
  `query_max_bytes` (default 64 KB). All truncation cuts on character boundaries;
  slicing UTF-8 at a byte offset produces invalid text.
- Every truncation is stated. A silently truncated result is a correctness bug in
  a tool that sells determinism.

### 6.4 Recovering a full payload

There is no `fetch` tool. Recovery is channel-aware, because `result_text` holds
only the prose when the payload came from `structuredContent`:

```sql
SELECT result_structured FROM sluice_calls WHERE call_id = '...'   -- structured
SELECT result_blocks     FROM sluice_calls WHERE call_id = '...'   -- text
```

`max_cell_bytes` (512 by default) truncates these long before the 64 KB output
cap, so a single select does not recover a large payload. Recovery is chunked and
the handle documents the pattern:

```sql
SELECT substr(result_text, 1, 4000) FROM sluice_calls WHERE call_id = '...'
```

`substr` on `VARCHAR` counts characters, so chunks are character-safe by
construction.

## 7. Configuration

`sluice.toml`, discovered by `--config`, then `$SLUICE_CONFIG`, then `./sluice.toml`.

```toml
[servers.gh]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }

[limits]
max_payload_bytes = 33554432   # see 5.1 on what this does and does not bound
max_concurrent_materializations = 2
preview_bytes = 2048
preview_rows = 3
max_columns = 64
query_timeout_seconds = 10
query_max_rows = 100
query_max_bytes = 65536
max_cell_bytes = 512
duckdb_max_memory = "1GB"
```

`max_concurrent_materializations` is an admission gate over the whole pipeline,
not just the write. Peak memory is a multiple of payload size, so N concurrent
materializations multiply it; the write lock alone does not bound this because
parsing and projection happen before the lock is taken.

## 8. Failure behavior

Four failure classes, recorded in `failure_class`. Revision 2 collapsed them and
promised verbatim passthrough that is not always achievable.

| Class | Cause | Behavior |
|---|---|---|
| `tool_error` | downstream returned `isError: true` | verbatim (FR-12) |
| `output_schema` | downstream declared an `outputSchema` and its result did not conform | the SDK raises `RuntimeError` inside `call_tool`; **there is no result object to forward**. Sluice returns its own `isError` result naming the downstream tool and the validation failure |
| `protocol` | JSON-RPC error from downstream | not an `isError` result; Sluice returns its own `isError` result carrying the code and message |
| `transport` | downstream process died, stream closed | Sluice returns its own `isError` result; the session is marked unhealthy |

Verified, SDK 2.1.1: `ClientSession.validate_tool_result` raises `RuntimeError`
when a tool with a cached output schema returns missing or non-conforming
structured content.

| Situation | Behavior |
|---|---|
| Envelope write fails | Log to stderr, return the downstream result unmodified, no row (FR-8) |
| Result is not JSON on any channel | Envelope-only handle, head-and-tail preview |
| Payload exceeds `max_payload_bytes` | Passthrough with a size note, payload columns NULL |
| Inference or insert fails | Envelope-only handle, `flat_reason` records the cause |
| `query` rejects the SQL | Tool error naming the reason, never a silent empty result |
| `query` times out | Tool error stating the timeout and elapsed budget |

The invariant: **Sluice never turns a working tool call into a failed one**, with
two boundaries, both named rather than implied:

1. **Process death.** An OOM kill during materialization ends the session, and no
   `except` catches it. The invariant is conditional on staying inside the memory
   budget, which `max_payload_bytes` and `max_concurrent_materializations` exist
   to bound, imperfectly (§5.1 step 2).
2. **Connection-scoped interrupt.** A timed-out `query` must not abort a
   concurrent write. §6.2.

## 9. Concurrency

- One writer connection. Writes serialized behind an `asyncio.Lock`.
- **One dedicated DuckDB connection per in-flight query**, closed when the query
  finishes. The timeout interrupts that exact object (§6.2).
- Materialization admission gated by `max_concurrent_materializations` (§7),
  covering parse and projection, not only the write.
- All DuckDB calls are blocking and dispatched through `asyncio.to_thread`.
- `<mounted>__latest` points at the highest `seq`, not the last write (§3.2).

## 10. Control plane

v0 declares its contract rather than leaving it implied.

- **Tool list**: a static catalog captured at startup. Sluice advertises
  `listChanged: false` and does not subscribe to downstream list changes. A
  downstream server that adds tools at runtime will not have them mounted until
  Sluice restarts. Stated, not silent.
- **Cancellation**: `notifications/cancelled` from upstream is forwarded
  downstream, and any DuckDB work in flight for that call is interrupted on its
  own connection. Cancelling an async handler does not by itself stop a blocking
  call already running in `to_thread`.
- **Progress**: not forwarded. Optional under the spec, so this is legal, and
  saying so is the difference between a choice and an oversight.
- **Capabilities**: per request, never inferred from connection state.

## 11. Multi-round-trip calls

FR-5 relays them. The hazard being avoided: if Sluice used the high-level client
with its own sampling or elicitation callbacks, **Sluice** would answer prompts
addressed to the real client and its human. So Sluice advertises no sampling or
elicitation capability of its own downstream, and passes `InputRequiredResult`,
`input_responses`, and the opaque `request_state` through untouched.

`request_state` is opaque and must be neither parsed nor rewritten.
Materialization runs only on a final `CallToolResult`.

## 12. Scope isolation

A client may reuse one stdio process across conversations, so process lifetime is
not conversation lifetime. Two failures follow if scope is ignored:

1. Conversation B reads conversation A's tables.
2. Worse: after a process restart, sequence numbers restart at 1, so a **resumed**
   conversation's stale handle can name a table that exists and holds different
   data. A clean answer about the wrong result set is the failure mode this
   project exists to prevent.

**The scope id.** If the client supplies a conversation identifier in request
`_meta`, Sluice derives the scope from it. Otherwise Sluice mints a fresh
unguessable scope id per proxied call. Either way the scope tag is embedded in
every table name (§3.2), so:

- A stale handle from a previous process names a table that cannot exist, and
  fails loudly (fix for failure 2, unconditional).
- A conversation can only name tables whose handles it was given, because names
  are unguessable and enumeration is blocked (FR-18, §6.1 layer 3).

**Residual risk, stated plainly.** Without a client-supplied conversation
identifier this is capability-based isolation, not enforced isolation. It rests on
unguessable names plus a catalog denylist, and a denylist is weaker than the other
two query layers. An agent that retains a handle across a conversation boundary
still reaches that table. Enforced isolation needs either a conversation id from
the client or one DuckDB database per scope, and both are v1.

The cost is FR-18: no table discovery. The agent's own transcript is the index.
Isolation and discovery are in direct tension and v0 chooses isolation.

## 13. Contradictions and decisions resolved

1. Handle names the flat tables and always the envelope row.
2. Homogeneity is a coverage model, not a binary test (§5.3).
3. "Read-only SQL" needs three layers, not a SELECT prefix check (§6.1).
4. "Statement timeout" is an interrupt on the exact executing connection (§6.2).
5. The correctness criterion is exact inside a defined domain and within
   tolerance outside it, never universally exact (§5.6).
6. One downstream server in v0; namespacing built, fan-out deferred (FR-7).
7. Always-intercept costs a round trip on small results; FR-14 answers it.
8. `structuredContent` is an input channel, not only an output one (§5.1).
9. The invariant has two boundaries, both named (§8).
10. §6.1's lockdown and a `read_json` load were mutually exclusive; materialization
    is file-free and Sluice owns inference (§5.4, §5.5).
11. Session scope was undefined; it is process scope plus a scope tag (§12).
12. Verbatim error passthrough is impossible for three of four failure classes (§8).
13. Silently choosing among several candidate arrays was incompatible with the
    correctness criterion; all candidates are materialized (§5.2).

## 14. Open questions

1. **Python floor 3.14 or 3.15.** 3.15 releases 2026-10-01. Gating factor is a
   DuckDB cp315 wheel. cp314 confirmed present at DuckDB 1.5.5.
2. **Repeated calls to the same tool** get their own tables plus a `__latest`
   view. Whether a union view is wanted is unknown until observed.
3. **Pagination of downstream results.** A tool that pages produces N tables the
   agent must UNION by hand.
4. **Enforced scope isolation** (§12), which needs a conversation id or a database
   per scope.
5. **Whether `_extra` and `JSON` columns are used** by a model in practice.
