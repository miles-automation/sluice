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
    tables, functions, shows, ctes = _walk(document)

    if shows:
        raise QueryRejectedError(
            "SHOW and DESCRIBE are not available; the tables you can query are named "
            "in the handles you were given"
        )
    forbidden_functions = sorted(functions - set(ALLOWED_TABLE_FUNCTIONS))
    if forbidden_functions:
        raise QueryRejectedError(f"table function not allowed: {', '.join(forbidden_functions)}")

    for schema, name in sorted(tables):
        if name in ctes and not schema:
            # A CTE alias appears in the AST as a base table. Allowing the names
            # bound by this same statement keeps `WITH x AS (...) SELECT * FROM x`
            # working without opening anything up.
            continue
        if schema:
            raise QueryRejectedError(f"schema-qualified tables are not available: {schema}.{name}")
        if name not in allowed_objects:
            raise QueryRejectedError(
                f"unknown table {name!r}. You can only query tables named in the handles "
                "you were given in this conversation."
            )


def _walk(node: Any) -> tuple[set[tuple[str, str]], set[str], bool, set[str]]:
    """Collect base tables, table functions, SHOW refs, and CTE names."""
    tables: set[tuple[str, str]] = set()
    functions: set[str] = set()
    ctes: set[str] = set()
    shows = False

    def visit(value: Any) -> None:
        nonlocal shows
        if isinstance(value, dict):
            kind = value.get("type")
            if kind == "BASE_TABLE":
                tables.add((value.get("schema_name") or "", str(value.get("table_name"))))
            elif kind == "TABLE_FUNCTION":
                function = value.get("function")
                if isinstance(function, dict):
                    functions.add(str(function.get("function_name")))
            elif kind == "SHOW_REF":
                shows = True
            cte_map = value.get("cte_map")
            if isinstance(cte_map, dict):
                for entry in cte_map.get("map") or []:
                    if isinstance(entry, dict) and entry.get("key"):
                        ctes.add(str(entry["key"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(node)
    return tables, functions, shows, ctes
