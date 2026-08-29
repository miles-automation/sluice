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
for a week, and nothing about querying materialized results needs them. Scalar
functions such as `json_extract` are unaffected: they are not table functions."""


class QueryRejectedError(ValueError):
    """The SQL did not pass the gate. Never executed."""


def check(sql: str, connection: duckdb.DuckDBPyConnection, allowed_objects: set[str]) -> None:
    """Raise `QueryRejectedError` unless `sql` is a single read over allowed objects."""
    _check_single_select(sql, connection)
    _check_objects(sql, connection, allowed_objects)


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
    sql: str, connection: duckdb.DuckDBPyConnection, allowed_objects: set[str]
) -> None:
    document = _serialize(sql, connection)
    _validate(document, frozenset(), allowed_objects)


def _cte_names(node: dict[str, Any]) -> frozenset[str]:
    cte_map = node.get("cte_map")
    if not isinstance(cte_map, dict):
        return frozenset()
    names = {
        str(entry["key"])
        for entry in cte_map.get("map") or []
        if isinstance(entry, dict) and entry.get("key")
    }
    return frozenset(names)


def _validate(node: Any, visible: frozenset[str], allowed_objects: set[str]) -> None:
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
        local = visible | _cte_names(node)
        kind = node.get("type")
        if kind == "BASE_TABLE":
            schema = node.get("schema_name") or ""
            name = str(node.get("table_name"))
            if schema:
                raise QueryRejectedError(
                    f"schema-qualified tables are not available: {schema}.{name}"
                )
            if name not in local and name not in allowed_objects:
                raise QueryRejectedError(
                    f"unknown table {name!r}. You can only query tables named in the "
                    "handles you were given in this conversation."
                )
        elif kind == "TABLE_FUNCTION":
            function = node.get("function")
            name = str(function.get("function_name")) if isinstance(function, dict) else "?"
            if name not in ALLOWED_TABLE_FUNCTIONS:
                raise QueryRejectedError(f"table function not allowed: {name}")
        elif kind == "SHOW_REF":
            raise QueryRejectedError(
                "SHOW and DESCRIBE are not available; the tables you can query are named "
                "in the handles you were given"
            )
        for child in node.values():
            _validate(child, local, allowed_objects)
    elif isinstance(node, list):
        for child in node:
            _validate(child, visible, allowed_objects)
