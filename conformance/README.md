# Sluice conformance suite

One language-neutral suite that every version of Sluice runs. The Python version
(`python/`) and the TypeScript version (`typescript/`) are measured against the
same fixtures, and CI runs both. A behavior change lands in both versions and in
a fixture here, or it is documented in the fixture's `note` as one-sided.

```
conformance/
  schema.json     JSON Schema (draft 2020-12) every fixture file must match
  fixtures/*.json one file per topic; each holds cases of one kind
  COVERAGE.md     every Python test mapped to a fixture or to a reason it stays Python-only
```

Runners:

- Python: `python/tests/test_conformance.py`, run by `uv run pytest` in `python/`.
- TypeScript: `typescript/test/conformance.test.ts`, run by `npm run check` in
  `typescript/`. A kind with no TypeScript implementation is registered as
  pending in `typescript/test/conformance/adapters.ts` and every one of its cases
  is reported as skipped with the task that will implement it. Replace the
  pending entry with an adapter to turn a kind on.

Both runners validate every file against `schema.json` and refuse duplicate case
ids before running anything.

## File format

```json
{
  "kind": "infer",
  "description": "what this file covers",
  "defaults": { "...": "merged under every case (gate and gate_ast only)" },
  "cases": [
    { "id": "infer.small-integers", "spec": "001 §5.5", "description": "...", "note": "...", "...": "kind-specific input and expect" }
  ]
}
```

Case ids are stable and globally unique: `<kind or topic>.<slug>`. Never reuse an
id for a different behavior; retire it and add a new one. `spec` points at the
section the case enforces. `note` records anything a porter needs to know,
including measured behavior that the spec does not state.

A case that holds for one version only carries
`"only": {"version": "python" | "typescript", "reason": "..."}`. The other
version's runner reports it as skipped with that reason. Use it for behavior the
spec leaves undecided, never to hide a failure.

### Rules that keep fixtures language-neutral

- **Wire JSON stays text.** Inputs a real implementation receives as JSON text
  (`payload_json`, `rows_json`, `values_json`, `structured_content_json`, text
  content blocks) are strings in the fixture. `1` and `1.0` are different on the
  wire (BIGINT versus DOUBLE), and integers beyond 2^53 must survive. A runtime
  whose default parser loses either needs a lossless parser for these inputs.
- **Plain JSON integers in expectations stay within ±(2^53 − 1).** The schema
  enforces it. Larger values use `{"int": "<digits>"}`.
- **Non-standard literals.** Python's `json` module accepts `Infinity`,
  `-Infinity` and `NaN` on the text channel. Spec 001 does not decide whether a
  text payload containing them is JSON, so the cases that rely on them are
  one-sided (`only: python`), and every other case is strict JSON.
- **No error text.** Rejections are asserted as a category plus, where it
  matters, the name of the offending object (`mentions`) or names that must not
  leak (`not_mentions`). Wording differs between DuckDB 1.5.5 and 2.x, and
  between Python and Node.
- **No implementation-defined numbers.** Retention charges are bounded, not
  fixed (spec 001 §7: S ≤ C ≤ 1.25·S + 1024), and retention cases are sized to
  hold anywhere in that range. The size of the query notice reserve is
  implementation-defined, so byte-cap cases assert that every row is accounted
  for rather than how many were shown.

## Kinds

| Kind | Boundary | Input | Expect |
|---|---|---|---|
| `extract` | §5.2 row-set extraction | `payload_json` | `row_sets` (path + rows) or `reason` category (`empty`, `mixed_elements`, `scalar`) plus `paths` named in it |
| `project` | §5.3 depth-1 projection | `rows_json`, `max_columns` | stored `columns` order, `renamed`, `extra_keys`, `records` (with `_row`, and `_extra` objects) |
| `infer` | §5.5 inference | `values_json` (one column) | `type`, `exact` |
| `naming` | §3.2 names | `op` (`slug`, `tag`, `mounted_name`, `collection_name`, `table_name`, `quote_ident`, `assert_injective`, `distinct`), `args` | `value`, `prefix`, `suffix`, `max_length`, `distinct`, `count` |
| `scope` | §12 scope ids | `op` (`from_conversation_id`, `derive`, `mint`), `conversation_id` or `meta` | `scope`, `from_client`, `pattern`, `distinct_over` (that many calls give distinct ids) |
| `cell` | §6.3 one rendered cell | `value`, `max_bytes` | `text`, `truncated` |
| `gate` | §6.1 the gate on a catalog | `sql`, `setup`, `allowed`, `evicted` | `decision` allow (and `executes`) or reject with `categories` |
| `gate_ast` | §6.1 the walker fails closed | `sql`, `mutations` | as `gate` |
| `engine` | engine facts as SQL | `setup`, `lockdown`, `locked_setup`, `sql` | `rows` (typed matchers), `nonempty`, `succeeds`, or `error` (`any`, `permission`) |
| `session` | a session: tool results in, handles and `query` results out | `limits`, `steps` | per step |
| `paginate` | spec 002 §4 page loop, and the collection tool | `config`, `arguments`, scripted `pages` | `expect` for the loop: `status`, `reason`, `pages`, `bytes`, `resume_offset`, `rows`, `calls`, or `argument_error`; `collection` for the tool result: see below |
| `config` | §7 and spec 002 §2 `sluice.toml` | `toml` or `toml_file` or `missing_file`, `environ` | `ok` with field subsets, or `error` with `mentions` |

### Gate

The runner opens a fresh in-memory DuckDB, runs `setup`, applies the spec 001
§6.1 layer-2 lockdown, then checks `sql` with `allowed` as the caller's
allowlist and `evicted` as names the session retention budget removed. `allow`
with `executes: true` also runs the SQL on that connection.

Rejection categories: `parse_error`, `no_statement`, `multiple_statements`,
`not_select`, `unanalyzable`, `table_function`, `function`, `schema_qualified`,
`unknown_table`, `evicted_table`, `show_describe`. The `query` tool adds
`invalid_argument`, `timeout` and `execution_error`. Where the category depends
on which layer refuses first, the case lists every acceptable one. That is the
case for non-SELECT statements and `PRAGMA`: Python on DuckDB 1.5.5 refuses
PRAGMA as unanalyzable, Python on 2.x refuses it as not SELECT, and Node types it
as SELECT through `prepare` (review/duckdb2-node-check.md). The TypeScript gate
takes the statement type from the parser instead: `json_serialize_sql` refuses
every statement that is not a query without binding it, PRAGMA included, so the
TypeScript gate refuses PRAGMA as not SELECT, and it calls `prepare` only after
the object check, as the review requires.

### Gate AST mutations

`gate_ast` serializes `sql` with the engine under test (`json_serialize_sql`),
applies `mutations` to the parsed document, and hands the result to the walker
in place of the real serialization. The statement layer still sees the original
SQL. Mutations are applied to the live serialization, so they hold for every
DuckDB AST format the walker supports. Operations:

| `op` | Fields | Effect |
|---|---|---|
| `update` | `find`, `set`, optional `within` | Depth-first, find the first object whose `type` equals `find` (under the path `within`, else the whole document), and assign the keys in `set`. |
| `set` | `path`, `value` | Assign `value` at `path`. |
| `delete` | `path` | Remove the key or array element at `path`. |
| `rename_first` | `path`, `keys`, `to` | At the object at `path`, move the first present key of `keys` to `to`. |
| `set_serialized` | `path`, `sql`, `value` | Serialize `sql`, take `statements[0].node`, substitute it for every `"$node"` string in `value`, and assign at `path`. |

| `replace_serialized` | `sql` | First mutation only. Start from the serialization of `sql` instead of the case's SQL, which is then never serialized. With `SELECT 1` this neutralizes the object layer, so only the statement layer can refuse. |

A path is a list of object keys and array indices from the document root.

### Engine

`setup` runs on a fresh in-memory database, then `lockdown: true` applies the
spec 001 §6.1 layer-2 settings, then `locked_setup` runs on the locked engine.
Claims about what works "after the lockdown" put their statements in
`locked_setup`.

### Pagination and the collection tool

`pages` maps each offset the loop requests (as a decimal string) to a page: text
content with optional `is_error`, a downstream `failure` class, or
`input_required`. `expect` checks the loop itself. `collection` checks what the
agent gets from the collection tool for the same pages, under default limits:

- `{"outcome": "error"}`: a tool error with no handle and no table. A port that
  returns an empty table or a zero-row handle for a fetch with no page fails
  here (spec 002 PG-12). `keeps_downstream_error: true` means the downstream
  error's own text comes first, followed by Sluice's note.
- `{"outcome": "handle", "table_rows": n, "status", "reason", "resume_offset"}`:
  one table with `n` rows, and the handle's `pagination` object carries the
  status.
- `{"outcome": "envelope_only"}`: pages were fetched but held no rows, so the
  handle names no table.

### Matchers

Query cells (`session`) and engine values (`engine`) use the same matcher syntax.
Query cells are compared as rendered text; engine values are compared with their
type.

| Matcher | Query cell | Engine value |
|---|---|---|
| `"text"` | rendered text equals it (escaped per §6.3) | a string equal to it |
| `null` | `NULL` | SQL NULL |
| `42` | an integer literal with that value | an integer |
| `{"int": "<digits>"}` | an integer literal, arbitrary precision | an integer, arbitrary precision |
| `{"double": n}` (number or numeric string) | parses to the same binary64 value | a float with that value |
| `{"approx": x, "rel": r, "abs": a}` | within `math.isclose(rel_tol=r, abs_tol=a)`; with `"exact": false` it must also differ from `x` | same, on a float |
| `{"bool": b}` | `true`, `false`, `True` or `False` | a boolean |
| `{"decimal": "12.50"}` | exactly that text | a DECIMAL value whose text is exactly that |

Numeric cell text must match `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`
before it is compared; `nan`, `inf`, digit separators and surrounding spaces
never match. A DECIMAL value never matches a `double` matcher.

Spec 001 §6.3 does not define how BOOLEAN or DOUBLE values render: Python prints
`True` and `2.0` where Node prints `true` and `2`. The matchers accept both, and
fixtures avoid rendering lists, JSON and timestamps directly.

### Session

A session case opens a fresh store with `limits` (spec 001 §7 field names;
unset fields take their defaults) and runs `steps` in order. Each call step is
intercepted as a call to tool `tool` (default `"tool"`) on server `server`
(default `"conformance"`), with `arguments` and request `meta`. The query tool is
mounted.

A call step's `result` is the downstream `CallToolResult`: `content` blocks,
optional `structured_content_json`, optional `is_error`. `expect` is either:

- `{"outcome": "passthrough", "passthrough": "is_error" | "non_text" | "oversize", "size_note": bool}`:
  the original result comes back with its content, structured content and error
  flag unchanged; oversize appends exactly one text block (the size note).
- `{"outcome": "handle", ...}`: the result is a handle. Checked against its
  structured content: `source_channel`, `channel_conflict`, `scope_id`,
  `preview_complete`, `preview_equals`, `preview_contains`, `preview_excludes`,
  `flat_reason` (the category: the text before the first `:` or `;`, or null),
  and `tables` (`source_path`, `row_count`, and `columns` exactly, or
  `column_count` and `columns_include`). Every table name must appear in the
  text content. `preview_rows` and `preview_total_rows` read the spec 001 §4.1
  `preview (first N of M rows` line; `text_contains` and `text_max_bytes` check
  the text content.

A query step's `query` holds the raw `query` tool arguments. String values may
use placeholders, replaced with the bare name: `{{c1.table}}` (first table of
call `c1`), `{{c1.table.N}}`, `{{c1.envelope}}` (the call's scope view),
`{{c1.call_id}}`, `{{c1.scope}}`. A passthrough call has no handle, so its
envelope placeholder needs a conversation id in `meta` to derive the scope.

A query step expects either `{"error": true, "categories": [...]}` (categories
are required), which must be a tool error with no table and no rows-shown
notice, or a non-empty set of checks on a result. The result's notice line is
parsed strictly against the vocabulary spec 001 §6.3 fixes; a sentence the
runner does not recognize fails the case, so an absent notice really is absent.
The checks, in language-neutral form: `columns`, `rows`, `row_count`, `shown` (rows rendered),
`more_rows` (rows exist beyond `max_rows`), `rows_omitted` (dropped for the byte
cap), `rows_omitted_min`, `accounted_rows` (shown plus omitted), `cells_truncated`,
`headers_truncated`, `table_omitted`, `headers_truncated_or_table_omitted`,
`notices` (the exact kinds present, in order: `shown`, `more_rows`,
`rows_omitted`, `cells_truncated`, `headers_truncated`, `table_omitted`,
`fallback`), and `text_excludes`. Every successful result must fit `query_max_bytes` in UTF-8
bytes and must carry its notices.

## Adding a case

1. Add it to the fixture file for its topic, with a new id and its `spec`.
2. Run both runners: `cd python && uv run pytest tests/test_conformance.py` and
   `cd typescript && npm run check`.
3. If it replaces a Python test, update `COVERAGE.md`.
