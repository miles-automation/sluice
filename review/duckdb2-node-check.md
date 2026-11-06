# DuckDB 2.0 from Node: primitive check (task 1074)

Date: 2026-11-06. Package `@duckdb/node-api` 2.0.0-r.1, engine v2.0.0. Script and
data: `spike/duckdb2/`. Control: Python `duckdb` 1.5.5 and 2.0.0, `mcp` 2.3.0.

## Verdict

Every primitive Sluice needs exists in Node on DuckDB 2.0. The port can start,
with the design constraints below.

The check also found a live problem unrelated to the port. See the next section.

## Urgent: the published package has a gate bypass on DuckDB 2.0

`mcp-sluice` 0.2.0 declares `duckdb>=1.5.5` with no upper bound. DuckDB 2.0.0 was
uploaded to PyPI on 2026-10-21, so a fresh install today gets it. Verified with a
clean `uv run --with mcp-sluice`: it resolved 0.2.0 on DuckDB 2.0.0, and
`gate.check` admitted `WITH x AS (SELECT * FROM sluice_calls) SELECT * FROM x`.

Cause: 2.0 moved a CTE body from `value.query.node` to `value.query_node`.
`gate.py` reads the old path, finds nothing, skips the body, and then deliberately
does not walk `cte_map` again. Anything inside a CTE body is therefore unchecked.

Admitted on 2.0.0, refused on 1.5.5, through the real `gate.check`:

- `WITH x AS (SELECT * FROM sluice_calls) SELECT * FROM x` (reads every scope's
  envelope, breaking isolation, spec 3.3)
- `WITH x AS (SELECT * FROM sqlite_master) SELECT * FROM x` (table discovery)
- `WITH x AS (SELECT * FROM range(100000000000)) SELECT count(*) FROM x` (blocked
  allocation function)
- `WITH RECURSIVE r(n) AS (SELECT 1 FROM sluice_calls UNION ALL SELECT n+1 FROM r) SELECT * FROM r`

Still refused without a CTE: direct references, subqueries, table functions,
`SHOW`, `DESCRIBE`, `SUMMARIZE`, `PRAGMA`. The engine lockdown still holds, so file
access stays blocked on both versions. The exposure is cross-scope reads and
resource use by the agent, not the filesystem.

Not fixed here; this task is a check. Options for the owner:

1. Release 0.2.1 with the fix below. Tested in a throwaway copy: on 2.0.0 all six
   bypass tests pass, leaving only the four PRAGMA tests described under "Python
   baseline"; on 1.5.5 all 433 pass.
2. Release 0.2.1 that pins `duckdb<2`. Smallest change, but goes against the
   no-upper-pins policy.
3. Do both, or yank 0.2.0.

```python
query = value.get("query")
query_node = query.get("node") if isinstance(query, dict) else value.get("query_node")
if not isinstance(query_node, dict):
    raise QueryRejectedError("the SQL could not be analyzed: unknown CTE shape")
```

The last two lines are the real lesson: the walker fails open on a shape it does
not recognize. That is the opposite of what an allowlist is for.

## Primitives

Same result on darwin-arm64 (node 25.3.0) and linux-arm64 (node 24.22.2, Docker):
15 of 15 checks pass. linux-amd64 was not run; it needs QEMU or Rosetta here, so it
should be covered by the CI matrix in task 1075.

| Primitive | Verdict | Notes |
| --- | --- | --- |
| Statement typing | Pass, differs | `extractStatements` counts without binding. The type needs `prepare`, which binds |
| `json_serialize_sql` | Pass, differs | Output format changed. See AST changes |
| `enable_external_access=false` | Pass | `read_text`, `read_json`, `read_csv`, file paths, `COPY`, `ATTACH`, `INSTALL`, `LOAD` all refused |
| `lock_configuration=true` | Pass | Refuses changes on this and new connections, including `RESET` |
| `max_memory` | Pass | Loud `Out of Memory Error`, store usable afterwards |
| Interrupt | Pass | Ends a running query in about 200 ms, connection usable afterwards, event loop free, other connections unaffected. Also stops an unbounded recursive CTE |
| One connection per query | Pass | A query finishes while another runs. Transaction visibility holds |
| Median over typed columns | Pass | BIGINT, DOUBLE, DECIMAL correct. Text and JSON still lexicographic, so spec 5.5 still applies |
| File-free materialization | Pass | Appender and prepared inserts work under the lockdown |

## Constraints for the port

1. **Gate order changes.** Python types a statement without binding. In Node, type
   comes from `prepare`, which binds, so an unknown table fails there with a
   different error than a known one. Typing first would make a table-existence
   oracle. Run the AST allowlist first, then `prepare`. Do not rely on the type
   alone: Node reports `PRAGMA show_tables` as SELECT (Python 2.0 reports PRAGMA).
   It is still refused, because it does not serialize.
2. **Empty input.** `extractStatements` on empty or comment-only SQL throws an opaque
   `Error in native callback`, where Python returns an empty list. It fails closed;
   the port should turn it into "no statement found".
3. **Fail closed on unknown AST shapes.** Add a startup canary that serializes a known
   CTE and refuses to run if the expected path is missing, and pin the DuckDB version
   exactly in the npm package.
4. **The libuv pool is shared, and `connect()` uses it too.** With every pool thread
   (default 4) in a slow query, new queries and even `connect()` wait. An early
   version of this check hung by awaiting `connect()` before calling `interrupt()`.
   `interrupt()` itself is synchronous and always gets through. So: deadlines run on
   timers and never await a DuckDB call first; connect before starting the query;
   admission caps in-flight queries below `UV_THREADPOOL_SIZE`, which must be set
   before the first pool use (in the environment for a separate process).
5. **Render values from `getRows()`, not `getRowsJS()`.** `getRowsJS` turned DECIMAL
   12345678901234567.12 into 12345678901234568. `String()` over `getRows()` is exact
   for BIGINT, HUGEINT, DECIMAL and DOUBLE, and keeps NULL distinct from the empty
   string. This matters to a tool that sells exact answers.
6. **`JSON.parse` of the AST rounds very large integers.** `query_location` becomes
   18446744073709552000. The gate does not read it. Ignoring that field, Node and
   Python 2.0 produce identical ASTs on all 48 statements.

## AST changes from 1.5.5 to 2.0.0

48 statements compared (`spike/duckdb2/statements.json`); 38 changed, on structure
or field names, and no node `type` values were added or removed. Identical on
darwin-arm64 and linux-arm64.

Fields `gate.py` depends on:

| Field | 1.5.5 | 2.0.0 | Effect on the gate |
| --- | --- | --- | --- |
| CTE body | `value.query.node` | `value.query_node` | **Breaks: bypass** |
| Function args | `children` | `arguments[].expression` | None, the walker is generic |
| Constants | `value: {is_null, type, value}` | `literal: {kind, text}` | None today, but a `value`/`type` key no longer sits under constants |
| `BASE_TABLE` | `schema_name`, `table_name` | same, plus `qualified_name.path` | None |
| `TABLE_FUNCTION.function.function_name` | present | present | None |
| `FUNCTION.function_name` | present | present | None |
| `RECURSIVE_CTE_NODE.left`, `.right` | present | present | None once the CTE body is found |
| `SHOW_REF` | `show_type: SHOW_UNQUALIFIED` | `SHOW_SPECIAL` | None, the gate matches the type |
| Subquery | `subquery.node`, `named_param_map` | `subquery.node` | None |

Other additions: `query_location_length` on most nodes, `payload_aggregates` on CTE
entries, `subquery_type` replacing `comparison_type` on subqueries. Non-SELECT
statements still serialize to `error: true`. `PIVOT` now types as MULTI in Node, not
as two statements; refused either way.

## Python baseline

The existing suite, run once in a throwaway copy with no project changes:

- DuckDB 1.5.5 (control): 433 passed.
- DuckDB 2.0.0: 423 passed, 10 failed.

Six are the bypass above, all `DID NOT RAISE QueryRejectedError`:

- `test_enumeration_and_filesystem_vectors_are_rejected[WITH x AS (SELECT * FROM duckdb_tables()) ...]`
- `test_cte_binding_order_cannot_shadow_physical_tables` (3 cases)
- `test_shadowing_variants_are_all_rejected` (2 cases)

Four are not security problems. Python 2.0 now types `PRAGMA` as PRAGMA instead of
SELECT, so it is refused one layer earlier with a different message:

- `test_pragmas_are_rejected` (3 cases: expects "could not be analyzed", gets "only SELECT is allowed")
- `test_the_statement_gate_alone_would_pass_these` (asserts the old type)

## Native size

| Platform | Binding | Contents |
| --- | --- | --- |
| darwin-arm64 | 63 MB | `libduckdb.dylib` 65.7 MB (decimal), `duckdb.node` 0.5 MB |
| linux-arm64 | 96 MB | `libduckdb.so` 99.9 MB (decimal), `duckdb.node` 0.6 MB |
| linux-x64 | 96 MB (npm unpacked size) | not installed here |

`@duckdb/node-api` adds 1.7 MB. npm installs both the glibc and musl bindings on
Linux from the lockfile, so `node_modules` there is 188 MB.

## Slopticus bundling

`mac/build.sh` bundles with esbuild into Node SEA executables (Node 24.21.0). A native
addon cannot be embedded: it has to be a real file on disk. Tested with that exact
Node, a real SEA blob and `--options runtime` plus Slopticus's `mac/entitlements.plist`:
loading through `createRequire` with a path beside the executable works, and DuckDB
ran with the lockdown enforced. Not a blocker.

- The entitlements already include `disable-library-validation` and `allow-jit`.
  Without them the SEA itself crashes at V8 startup, which has nothing to do with DuckDB.
- About 65 MB goes into `Contents/` (`libduckdb.dylib`, `duckdb.node`, plus the JS).
  Both native files need signing during the build and have to go through notarization.
  Only ad-hoc signing was tested here. Notarization is unverified.
- esbuild must keep `@duckdb/*` external, and it has to load through `createRequire`.
- Sluice should run as its own process, for example an `slopticus-adapter` mode, so no
  new 120 MB Node binary is needed. A native crash then cannot take down the service,
  and `UV_THREADPOOL_SIZE` can be set for that process alone.

## Stability

- `2.0.0-r.1` is the npm `latest` tag. `-r.N` is this package's revision suffix (used
  on 1.5.x as well), not a pre-release marker. The only other tag is `lts-v1.4`.
- There is one 2.0 revision so far, published 2026-10-21, so it is about two weeks old.
  PyPI has 2.0.0 and only dev builds of later versions.
- The AST format is not a stable interface: it changed in structure between 1.5.5 and
  2.0.0 with no node type changes to signal it. Hence constraint 3.

## Missing primitives

None. Nothing blocks the port. The decisions for the owner are the urgent item above,
and whether the port's first release pins DuckDB exactly (recommended).
