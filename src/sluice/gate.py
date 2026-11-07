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
all as SELECT, so the statement gate passes them. DuckDB 2.0 types PRAGMA as
PRAGMA and still types the other two as SELECT.

The walker fails closed. The serialized AST changes between DuckDB releases, so a
shape it does not recognize in a position that can hold a query or a table is
refused, and `verify_parser_contract` refuses to open a session at all if a known
table can no longer be found.
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


QUERY_NODE_TYPES: frozenset[str] = frozenset(
    {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"}
)
"""Every query node shape the walker understands. Anything else is refused."""

TABLE_REF_TYPES: frozenset[str] = frozenset(
    {
        "BASE_TABLE",
        "EMPTY",
        "EXPRESSION_LIST",
        "JOIN",
        "PIVOT",
        "SHOW_REF",
        "SUBQUERY",
        "TABLE_FUNCTION",
    }
)
"""Every table reference shape the walker understands. Anything else is refused."""

_CANARY_TABLE = "sluice_gate_canary"
_CANARY_REFUSALS: tuple[str, ...] = (
    f"SELECT * FROM {_CANARY_TABLE}",
    f"WITH c AS (SELECT * FROM {_CANARY_TABLE}) SELECT * FROM c",
    f"WITH RECURSIVE c AS (SELECT * FROM {_CANARY_TABLE} UNION ALL SELECT * FROM c) "
    "SELECT * FROM c",
    f"SELECT 1 UNION ALL SELECT * FROM {_CANARY_TABLE}",
    f"SELECT * FROM (SELECT * FROM {_CANARY_TABLE}) s",
    f"SELECT 1 WHERE EXISTS (SELECT * FROM {_CANARY_TABLE})",
    f"SELECT 1 FROM (SELECT 1) a JOIN {_CANARY_TABLE} b ON true",
)
_CANARY_ADMITTED = "WITH c AS (SELECT * FROM {table}) SELECT * FROM c"
_CANARY_FUNCTION_REFUSALS: tuple[str, ...] = (
    "SELECT sleep_ms(1)",
    "SELECT pg_get_viewdef(0)",
    "SELECT pg_sleep(0)",
)
_MACRO_QUERY_RE = re.compile(r"\b(?:select|from)\b", re.IGNORECASE)
_MACRO_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


class QueryRejectedError(ValueError):
    """The SQL did not pass the gate. Never executed."""


class GateContractError(RuntimeError):
    """This DuckDB's parser output is not one the gate can enforce. Refuse to run."""


def check(
    sql: str,
    connection: duckdb.DuckDBPyConnection,
    allowed_objects: set[str],
    evicted_objects: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Raise `QueryRejectedError` unless `sql` is a single read over allowed objects."""
    _check_single_select(sql, connection)
    _check_objects(sql, connection, allowed_objects, evicted_objects)


def verify_parser_contract(connection: duckdb.DuckDBPyConnection) -> None:
    """Refuse to run on a DuckDB whose parser output the walker cannot read.

    The AST is not a stable format. DuckDB 2.0 moved CTE bodies, and a walker
    that skipped what it did not recognize admitted everything inside them. Prove
    on every session open that a table in each query position is still found.
    """
    for sql in _CANARY_REFUSALS:
        try:
            check(sql, connection, set())
        except QueryRejectedError as exc:
            if _CANARY_TABLE in str(exc):
                continue
            raise GateContractError(_contract_failure(sql, str(exc))) from exc
        raise GateContractError(_contract_failure(sql, "the query was admitted"))
    for sql in _CANARY_FUNCTION_REFUSALS:
        try:
            check(sql, connection, set())
        except QueryRejectedError as exc:
            if "function not allowed" in str(exc):
                continue
            raise GateContractError(_contract_failure(sql, str(exc))) from exc
        raise GateContractError(_contract_failure(sql, "the query was admitted"))
    sql = _CANARY_ADMITTED.format(table=_CANARY_TABLE)
    try:
        check(sql, connection, {_CANARY_TABLE})
    except QueryRejectedError as exc:
        raise GateContractError(_contract_failure(sql, str(exc))) from exc


def _contract_failure(sql: str, outcome: str) -> str:
    return (
        f"Sluice's query gate cannot read the parser output of DuckDB {duckdb.__version__}, "
        f"so it refuses to run. Probe {sql!r}: {outcome}. "
        "Upgrade Sluice or install a DuckDB version it supports."
    )


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
    restricted_functions = _restricted_macros(
        connection, table_function_names | MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS
    )
    statements = document.get("statements")
    if not isinstance(statements, list) or not statements:
        raise QueryRejectedError("the SQL could not be analyzed: no statements in the parse")
    for statement in statements:
        node = statement.get("node") if isinstance(statement, dict) else None
        _require_query_node(node)
    _validate(document, frozenset(), allowed, evicted, restricted_functions)


def _restricted_macros(
    connection: duckdb.DuckDBPyConnection, restricted: frozenset[str]
) -> frozenset[str]:
    macros: dict[str, str] = {}
    for name, definition in connection.execute(
        "SELECT function_name, macro_definition FROM duckdb_functions() "
        "WHERE function_type = 'macro'"
    ).fetchall():
        key = str(name).casefold()
        macros[key] = macros.get(key, "") + " " + str(definition or "")
    denied = set(restricted)
    changed = True
    while changed:
        changed = False
        for name, definition in macros.items():
            if name in denied:
                continue
            calls = {m.casefold() for m in _MACRO_CALL_RE.findall(definition)}
            if _MACRO_QUERY_RE.search(definition) or calls & denied:
                denied.add(name)
                changed = True
    return frozenset(denied)


def _unreadable(what: str) -> QueryRejectedError:
    """Refuse what the walker cannot read. Skipping it would admit whatever is inside."""
    return QueryRejectedError(f"the SQL could not be analyzed: {what}")


def _require_query_node(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict) or node.get("type") not in QUERY_NODE_TYPES:
        kind = node.get("type") if isinstance(node, dict) else type(node).__name__
        raise _unreadable(f"unrecognized query node {kind!r}")
    return node


def _require_table_ref(node: Any) -> None:
    if not isinstance(node, dict) or node.get("type") not in TABLE_REF_TYPES:
        kind = node.get("type") if isinstance(node, dict) else type(node).__name__
        raise _unreadable(f"unrecognized table reference {kind!r}")


def _cte_body(value: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a CTE's query node and the key it was found under.

    DuckDB 1.5 serializes the body at `query.node`, DuckDB 2.0 at `query_node`.
    """
    if "query_node" in value:
        return _require_query_node(value["query_node"]), "query_node"
    query = value.get("query")
    if isinstance(query, dict):
        return _require_query_node(query.get("node")), "query"
    raise _unreadable("a CTE has no body the gate can read")


def _check_query_positions(node: dict[str, Any], kind: Any) -> None:
    """Refuse an unknown shape in any position that can hold a query or a table."""
    if "from_table" in node:
        _require_table_ref(node["from_table"])
    if kind == "JOIN":
        _require_table_ref(node.get("left"))
        _require_table_ref(node.get("right"))
    elif kind == "PIVOT":
        _require_table_ref(node.get("source"))
    elif kind == "SET_OPERATION_NODE":
        children = node.get("children")
        members = [node.get("left"), node.get("right"), *(children or [])]
        present = [member for member in members if member is not None]
        if len(present) < 2:
            raise _unreadable("a set operation has no readable members")
        for member in present:
            _require_query_node(member)
    elif kind == "RECURSIVE_CTE_NODE":
        _require_query_node(node.get("left"))
        _require_query_node(node.get("right"))
    if "subquery" in node:
        subquery = node["subquery"]
        _require_query_node(subquery.get("node") if isinstance(subquery, dict) else None)


def _cte_entries(node: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if "cte_map" not in node:
        return []
    cte_map = node["cte_map"]
    declared = cte_map.get("map") if isinstance(cte_map, dict) else None
    if not isinstance(declared, list):
        raise _unreadable("a CTE list has an unrecognized shape")
    entries: list[tuple[str, dict[str, Any]]] = []
    for entry in declared:
        if not (
            isinstance(entry, dict) and entry.get("key") and isinstance(entry.get("value"), dict)
        ):
            raise _unreadable("a CTE entry has an unrecognized shape")
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
    left = _require_query_node(node.get("left"))
    _validate(left, visible, allowed_objects, evicted_objects, restricted_functions)
    right = _require_query_node(node.get("right"))
    _validate(right, visible | {name}, allowed_objects, evicted_objects, restricted_functions)
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
            query_node, body_key = _cte_body(value)
            if query_node.get("type") == "RECURSIVE_CTE_NODE":
                _validate_recursive_cte(
                    query_node,
                    local,
                    name,
                    allowed_objects,
                    evicted_objects,
                    restricted_functions,
                )
            else:
                _validate(
                    query_node,
                    local,
                    allowed_objects,
                    evicted_objects,
                    restricted_functions,
                )
            # Key targets and payload aggregates are expressions, so they can hold
            # subqueries and function calls too.
            for key, child in value.items():
                if key == body_key:
                    continue
                _validate(child, local, allowed_objects, evicted_objects, restricted_functions)
            local = local | {name}
        kind = node.get("type")
        _check_query_positions(node, kind)
        if kind == "BASE_TABLE":
            raw_name = node.get("table_name")
            if not isinstance(raw_name, str) or not raw_name:
                raise _unreadable("a table reference has no name")
            name = raw_name
            schema = node.get("schema_name") or ""
            catalog = node.get("catalog_name") or ""
            if schema or catalog:
                qualifier = ".".join(part for part in (catalog, schema) if part)
                raise QueryRejectedError(
                    f"schema-qualified tables are not available: {qualifier}.{name}"
                )
            qualified = node.get("qualified_name")
            path = qualified.get("path") if isinstance(qualified, dict) else None
            if path is not None and path != [name]:
                raise QueryRejectedError(
                    f"schema-qualified tables are not available: {'.'.join(map(str, path))}"
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
            raw_name = node.get("function_name")
            if not isinstance(raw_name, str) or not raw_name:
                raise _unreadable("a function call has no name")
            name = raw_name
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
