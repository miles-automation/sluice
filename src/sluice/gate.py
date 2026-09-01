"""The read-only gate for agent SQL (spec 6.1).

Pure-ish module: it needs a DuckDB connection to borrow the engine's parser, but
it never executes the SQL it inspects.

Three layers, because no one of them is sufficient:

1. **Statement gate.** Exactly one statement, of type SELECT.
2. **Engine lockdown.** Applied once at session open in `store.py`.
3. **Object allowlist.** Every table the statement references must be one Sluice
   created.

Layer 3 is an allowlist and not the denylist the spec first specified. A
denylist has to enumerate `duckdb_tables()`, `duckdb_columns()`,
`information_schema`, `pg_catalog`, `sqlite_master`, and whatever the next
DuckDB release adds. An allowlist is closed by construction: an object Sluice
did not create is refused whether or not anyone thought of it.

It also closes three vectors layer 1 does not. Measured on DuckDB 1.5.5,
`extract_statements` types `SHOW TABLES`, `DESCRIBE x`, and `PRAGMA show_tables`
all as SELECT, so the statement gate passes them.
"""

import json
import re
from typing import Any

import duckdb

SCOPE_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

ALLOWED_TABLE_FUNCTIONS: frozenset[str] = frozenset()
"""No table functions. `range()` and friends are how you write a query that runs
for a week, and nothing about querying materialized results needs them. Common
bounded scalar functions such as `json_extract` remain available; scalar calls
with a table-function overload or attacker-chosen allocation size do not."""

MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS: frozenset[str] = frozenset(
    {
        "array_resize",
        "bitstring",
        "format",
        "list_resize",
        "lpad",
        "nextval",
        "printf",
        "rpad",
        "setseed",
        "sleep_ms",
        "write_log",
    }
)
"""Scalar calls that mutate, block, or allocate from an attacker-chosen size."""


class QueryRejectedError(ValueError):
    """The SQL did not pass the gate. Never executed."""


def check(
    sql: str,
    connection: duckdb.DuckDBPyConnection,
    allowed_objects: set[str],
    evicted_objects: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Raise `QueryRejectedError` unless `sql` is a single read over allowed objects."""
    _check_single_select(sql, connection)
    _check_objects(sql, connection, allowed_objects, evicted_objects)


def _check_single_select(sql: str, connection: duckdb.DuckDBPyConnection) -> None:
    try:
        statements = connection.extract_statements(sql)
    except duckdb.Error as exc:
        raise QueryRejectedError(f"could not parse the SQL: {exc}") from exc
    if not statements:
        raise QueryRejectedError("no statement found")
    if len(statements) > 1:
        kinds = ", ".join(s.type.name for s in statements)
        raise QueryRejectedError(
            f"{len(statements)} statements found ({kinds}); query takes exactly one"
        )
    kind = statements[0].type.name
    if kind != "SELECT":
        raise QueryRejectedError(f"only SELECT is allowed, got {kind}")


def _serialize(sql: str, connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        raw = connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0]
    except duckdb.Error as exc:
        # Fails closed. `PRAGMA show_tables` lands here: it types as SELECT but
        # cannot be serialized, so refusing anything unserializable removes the
        # whole PRAGMA family without naming any of them.
        raise QueryRejectedError(f"the SQL could not be analyzed: {exc}") from exc
    document: dict[str, Any] = json.loads(raw)
    if document.get("error"):
        raise QueryRejectedError(f"the SQL could not be analyzed: {document.get('error_message')}")
    return document


def _check_objects(
    sql: str,
    connection: duckdb.DuckDBPyConnection,
    allowed_objects: set[str],
    evicted_objects: set[str] | frozenset[str],
) -> None:
    document = _serialize(sql, connection)
    # DuckDB resolves unquoted identifiers case-insensitively. Normalize both
    # sides so a CTE cannot evade or accidentally gain the allowlist merely by
    # changing its spelling.
    allowed = frozenset(name.casefold() for name in allowed_objects)
    evicted = frozenset(name.casefold() for name in evicted_objects)
    # Some DuckDB table functions also have scalar syntax. In particular,
    # `SELECT unnest(range(...))` does not produce a TABLE_FUNCTION AST node,
    # so checking that node alone leaves a resource-amplification bypass. Build
    # the deny set from the engine catalog rather than trying to keep a copy of
    # DuckDB's growing function list here.
    table_function_names = frozenset(
        str(row[0]).casefold()
        for row in connection.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table'"
        ).fetchall()
    )
    restricted_functions = table_function_names | MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS
    _validate(document, frozenset(), allowed, evicted, restricted_functions)


def _cte_entries(node: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    cte_map = node.get("cte_map")
    if not isinstance(cte_map, dict):
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for entry in cte_map.get("map") or []:
        if isinstance(entry, dict) and entry.get("key") and isinstance(entry.get("value"), dict):
            entries.append((str(entry["key"]).casefold(), entry["value"]))
    return entries


def _validate_recursive_cte(
    node: dict[str, Any],
    visible: frozenset[str],
    name: str,
    allowed_objects: frozenset[str],
    evicted_objects: frozenset[str],
    restricted_functions: frozenset[str],
) -> None:
    """Validate a recursive CTE with its seed and recursive terms scoped apart."""
    # The recursive binding is legal only in the recursive term. In particular,
    # the seed must not be able to use the same name to reach a physical table.
    left = node.get("left")
    if left is not None:
        _validate(left, visible, allowed_objects, evicted_objects, restricted_functions)
    right = node.get("right")
    if right is not None:
        _validate(
            right,
            visible | {name},
            allowed_objects,
            evicted_objects,
            restricted_functions,
        )
    for key, child in node.items():
        if key not in {"left", "right"}:
            _validate(child, visible, allowed_objects, evicted_objects, restricted_functions)


def _validate(
    node: Any,
    visible: frozenset[str],
    allowed_objects: frozenset[str],
    evicted_objects: frozenset[str],
    restricted_functions: frozenset[str],
) -> None:
    """Check every referenced object, honouring lexical scope.

    Scope is the whole point. Collecting CTE names globally and then allowing
    any base table with a matching name is a **complete bypass**: a CTE defined
    in a subquery whitelists that name for the entire statement, so

        SELECT scope_id, flat_tables FROM sluice_calls s
        WHERE EXISTS (WITH sluice_calls AS (SELECT 1) SELECT * FROM sluice_calls)

    reads the real envelope, and the same shape reaches `sqlite_master` and
    `duckdb_tables`. Verified against a build that collected names globally.

    A CTE name only permits references inside the node that defines it, where
    they resolve to the CTE rather than to anything real.
    """
    if isinstance(node, dict):
        # CTEs are sequential bindings. A declaration is visible to the main
        # query and later declarations, but not to its own definition or any
        # earlier definition. Treating the whole map as visible up front lets
        # `WITH hidden AS (SELECT * FROM hidden) ...` read a physical table.
        entries = _cte_entries(node)
        local = visible
        for name, value in entries:
            query = value.get("query")
            query_node = query.get("node") if isinstance(query, dict) else None
            if isinstance(query_node, dict) and query_node.get("type") == "RECURSIVE_CTE_NODE":
                _validate_recursive_cte(
                    query_node,
                    local,
                    name,
                    allowed_objects,
                    evicted_objects,
                    restricted_functions,
                )
            elif query_node is not None:
                _validate(
                    query_node,
                    local,
                    allowed_objects,
                    evicted_objects,
                    restricted_functions,
                )
            local = local | {name}
        kind = node.get("type")
        if kind == "BASE_TABLE":
            schema = node.get("schema_name") or ""
            name = str(node.get("table_name"))
            if schema:
                raise QueryRejectedError(
                    f"schema-qualified tables are not available: {schema}.{name}"
                )
            normalized_name = name.casefold()
            if normalized_name not in local and normalized_name in evicted_objects:
                raise QueryRejectedError(
                    f"table {name!r} was evicted by the session retention budget; "
                    "rerun the source tool call to materialize it again"
                )
            if normalized_name not in local and normalized_name not in allowed_objects:
                raise QueryRejectedError(
                    f"unknown table {name!r}. You can only query tables named in the "
                    "handles you were given in this conversation."
                )
        elif kind == "TABLE_FUNCTION":
            function = node.get("function")
            name = str(function.get("function_name")) if isinstance(function, dict) else "?"
            if name not in ALLOWED_TABLE_FUNCTIONS:
                raise QueryRejectedError(f"table function not allowed: {name}")
        elif kind == "FUNCTION":
            name = str(node.get("function_name") or "?")
            if name.casefold() in restricted_functions:
                raise QueryRejectedError(f"function not allowed: {name}")
        elif kind == "SHOW_REF":
            raise QueryRejectedError(
                "SHOW and DESCRIBE are not available; the tables you can query are named "
                "in the handles you were given"
            )
        # The CTE definitions were validated above with their declaration-time
        # visibility. Do not walk `cte_map` again with the final scope.
        for key, child in node.items():
            if key != "cte_map":
                _validate(child, local, allowed_objects, evicted_objects, restricted_functions)
    elif isinstance(node, list):
        for child in node:
            _validate(child, visible, allowed_objects, evicted_objects, restricted_functions)
