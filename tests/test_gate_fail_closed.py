"""The gate must fail closed when DuckDB's serialized AST is not what it expects.

DuckDB 2.0 moved CTE bodies from `value.query.node` to `value.query_node`. A walker
that read the old path and skipped what it did not find admitted everything inside
a CTE, so these tests pin both the exact bypass and the general rule behind it.
"""

import json
from collections.abc import Callable
from typing import Any

import duckdb
import pytest

from sluice import gate
from sluice.config import Limits
from sluice.gate import GateContractError, QueryRejectedError, check, verify_parser_contract
from sluice.store import Store

ALLOWED = {"mine", "other"}


@pytest.fixture
def connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE mine(a INTEGER, b VARCHAR)")
    con.execute("CREATE TABLE other(a INTEGER)")
    con.execute("CREATE TABLE sluice_calls(a INTEGER)")
    return con


def _reject(sql: str, con: duckdb.DuckDBPyConnection) -> str:
    with pytest.raises(QueryRejectedError) as caught:
        check(sql, con, ALLOWED)
    return str(caught.value)


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("WITH x AS (SELECT * FROM sluice_calls) SELECT * FROM x", "sluice_calls"),
        ("WITH x AS (SELECT * FROM sqlite_master) SELECT * FROM x", "sqlite_master"),
        ("WITH x AS (SELECT * FROM range(100000000000)) SELECT count(*) FROM x", "range"),
        ("WITH x AS (SELECT * FROM duckdb_tables()) SELECT * FROM x", "duckdb_tables"),
        (
            "WITH x AS (WITH y AS (SELECT * FROM sluice_calls) SELECT * FROM y) SELECT * FROM x",
            "sluice_calls",
        ),
        (
            "WITH x AS (SELECT 1), y AS (SELECT * FROM x, sluice_calls) SELECT * FROM y",
            "sluice_calls",
        ),
        (
            "SELECT * FROM (WITH x AS (SELECT * FROM sluice_calls) SELECT * FROM x) s",
            "sluice_calls",
        ),
        (
            "SELECT 1 WHERE EXISTS (WITH x AS (SELECT * FROM sluice_calls) SELECT * FROM x)",
            "sluice_calls",
        ),
        (
            "SELECT a FROM mine UNION ALL (WITH x AS (SELECT a FROM sluice_calls) SELECT a FROM x)",
            "sluice_calls",
        ),
        (
            "WITH x AS (SELECT a FROM mine UNION ALL SELECT a FROM sluice_calls) SELECT * FROM x",
            "sluice_calls",
        ),
        (
            "WITH RECURSIVE r AS (SELECT a FROM sluice_calls UNION ALL SELECT a FROM r) "
            "SELECT * FROM r",
            "sluice_calls",
        ),
        (
            "WITH RECURSIVE r AS (SELECT a FROM mine UNION ALL "
            "SELECT s.a FROM r, sluice_calls s) SELECT * FROM r",
            "sluice_calls",
        ),
        ("WITH x AS (SELECT sleep_ms(1)) SELECT * FROM x", "sleep_ms"),
        ("SELECT * FROM (PIVOT sluice_calls ON a IN (1) USING count(*))", "sluice_calls"),
        ("WITH x AS (SELECT * FROM main.mine) SELECT * FROM x", "schema-qualified"),
        ("SELECT * FROM memory.main.mine", "schema-qualified"),
    ],
)
def test_nothing_hides_inside_a_cte_or_nested_query(
    sql: str, fragment: str, connection: duckdb.DuckDBPyConnection
) -> None:
    assert fragment in _reject(sql, connection)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM mine",
        "WITH x AS (SELECT * FROM mine) SELECT * FROM x",
        "WITH x AS (SELECT a FROM mine), y AS (SELECT * FROM x) SELECT * FROM y",
        "WITH x AS (WITH y AS (SELECT a FROM mine) SELECT * FROM y) SELECT * FROM x",
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT * FROM r",
        "SELECT a FROM mine UNION ALL SELECT a FROM other",
        "SELECT a FROM mine UNION SELECT a FROM other EXCEPT SELECT 1",
        "SELECT * FROM mine UNION ALL BY NAME SELECT * FROM other",
        "SELECT * FROM mine m JOIN other o USING (a)",
        "SELECT * FROM mine, LATERAL (SELECT mine.a + 1)",
        "SELECT * FROM (SELECT a FROM mine) s",
        "SELECT * FROM mine WHERE a IN (SELECT a FROM other)",
        "SELECT (SELECT max(a) FROM other) FROM mine",
        "SELECT * FROM (VALUES (1, 2)) v(x, y)",
        "SELECT 1",
        "FROM mine",
        "SELECT * FROM mine QUALIFY row_number() OVER (ORDER BY a) = 1",
        "SELECT b, count(*) FROM mine GROUP BY b ORDER BY 2 DESC LIMIT 5",
        "SELECT * FROM mine TABLESAMPLE 10%",
        "SELECT * FROM (PIVOT mine ON b IN ('x') USING count(*))",
    ],
)
def test_legitimate_queries_are_still_admitted(
    sql: str, connection: duckdb.DuckDBPyConnection
) -> None:
    check(sql, connection, ALLOWED)
    connection.execute(sql).fetchall()


def _value_of_first_cte(document: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = document["statements"][0]["node"]["cte_map"]["map"][0]
    value: dict[str, Any] = entry["value"]
    return value


def _rename_cte_body(document: dict[str, Any]) -> None:
    value = _value_of_first_cte(document)
    value["future_body"] = value.pop("query_node") if "query_node" in value else value.pop("query")


def _drop_cte_map_list(document: dict[str, Any]) -> None:
    document["statements"][0]["node"]["cte_map"] = {"future_map": []}


def _cte_map_not_a_dict(document: dict[str, Any]) -> None:
    document["statements"][0]["node"]["cte_map"] = ["x"]


def _malformed_cte_entry(document: dict[str, Any]) -> None:
    document["statements"][0]["node"]["cte_map"]["map"][0] = {"name": "x"}


def _unknown_cte_body_type(document: dict[str, Any]) -> None:
    value = _value_of_first_cte(document)
    body = value["query_node"] if "query_node" in value else value["query"]["node"]
    body["type"] = "FUTURE_NODE"


def _empty_statements(document: dict[str, Any]) -> None:
    document["statements"] = []


def _no_statements_key(document: dict[str, Any]) -> None:
    del document["statements"]


def _unknown_statement_node(document: dict[str, Any]) -> None:
    document["statements"][0]["node"]["type"] = "FUTURE_NODE"


MUTATIONS: list[Callable[[dict[str, Any]], None]] = [
    _rename_cte_body,
    _drop_cte_map_list,
    _cte_map_not_a_dict,
    _malformed_cte_entry,
    _unknown_cte_body_type,
    _empty_statements,
    _no_statements_key,
    _unknown_statement_node,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda fn: fn.__name__)
def test_an_unknown_ast_shape_is_rejected(
    mutate: Callable[[dict[str, Any]], None],
    connection: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every SQL here is otherwise legal, so only the shape can refuse it."""
    real = gate._serialize

    def mutated(sql: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        document = json.loads(json.dumps(real(sql, con)))
        mutate(document)
        return document  # type: ignore[no-any-return]

    monkeypatch.setattr(gate, "_serialize", mutated)
    assert "could not be analyzed" in _reject(
        "WITH x AS (SELECT * FROM mine) SELECT * FROM x", connection
    )


def _find(node: Any, wanted: str) -> dict[str, Any]:
    """Depth-first search for the first dict whose `type` is `wanted`."""
    if isinstance(node, dict):
        if node.get("type") == wanted:
            return node
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        children = ()
    for child in children:
        try:
            return _find(child, wanted)
        except LookupError:
            continue
    raise LookupError(wanted)


@pytest.mark.parametrize(
    ("sql", "wanted", "change", "expected"),
    [
        (
            "SELECT * FROM mine",
            "SELECT_NODE",
            {"from_table": {"type": "FUTURE"}},
            "table reference",
        ),
        ("SELECT * FROM mine", "SELECT_NODE", {"from_table": 7}, "table reference"),
        ("SELECT * FROM mine m JOIN other o USING (a)", "JOIN", {"left": None}, "table reference"),
        ("SELECT * FROM mine", "BASE_TABLE", {"table_name": None}, "no name"),
        ("SELECT abs(1)", "FUNCTION", {"function_name": None}, "no name"),
        (
            "SELECT 1 UNION ALL SELECT 2",
            "SET_OPERATION_NODE",
            {"children": [], "left": None},
            "set",
        ),
        (
            "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n FROM r) SELECT * FROM r",
            "RECURSIVE_CTE_NODE",
            {"right": None},
            "query node",
        ),
        ("SELECT * FROM (SELECT 1) s", "SUBQUERY", {"subquery": {"future": {}}}, "query node"),
    ],
)
def test_a_missing_or_unknown_subtree_is_rejected_not_skipped(
    sql: str,
    wanted: str,
    change: dict[str, Any],
    expected: str,
    connection: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = gate._serialize

    def mutated(text: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(json.dumps(real(text, con)))
        _find(document, wanted).update(change)
        return document

    monkeypatch.setattr(gate, "_serialize", mutated)
    assert expected in _reject(sql, connection)


def test_expressions_beside_a_cte_body_are_walked(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key targets and payload aggregates hold expressions, which can hold queries."""
    real = gate._serialize

    def mutated(sql: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(json.dumps(real(sql, con)))
        hidden = real("SELECT * FROM sluice_calls", con)["statements"][0]["node"]
        _value_of_first_cte(document)["key_targets"] = [{"subquery": {"node": hidden}}]
        return document

    monkeypatch.setattr(gate, "_serialize", mutated)
    assert "sluice_calls" in _reject("WITH x AS (SELECT * FROM mine) SELECT * FROM x", connection)


def test_a_qualified_path_cannot_hide_behind_an_empty_schema(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate._serialize

    def mutated(sql: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(json.dumps(real(sql, con)))
        _find(document, "BASE_TABLE")["qualified_name"] = {"path": ["system", "mine"]}
        return document

    monkeypatch.setattr(gate, "_serialize", mutated)
    assert "schema-qualified" in _reject("SELECT * FROM mine", connection)


def test_a_catalog_alone_is_still_qualified(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate._serialize

    def mutated(sql: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(json.dumps(real(sql, con)))
        _find(document, "BASE_TABLE")["catalog_name"] = "system"
        return document

    monkeypatch.setattr(gate, "_serialize", mutated)
    assert "schema-qualified" in _reject("SELECT * FROM mine", connection)


def test_the_canary_passes_on_the_installed_duckdb(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    verify_parser_contract(connection)


def test_a_session_opens_with_the_canary_after_lockdown() -> None:
    with Store.open(Limits()) as store:
        verify_parser_contract(store.connection)


def test_the_canary_refuses_when_a_table_goes_unseen(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next format change: the walker admits a query it should have refused."""
    monkeypatch.setattr(gate, "_check_objects", lambda *_args: None)
    with pytest.raises(GateContractError) as caught:
        verify_parser_contract(connection)
    assert duckdb.__version__ in str(caught.value)
    assert "was admitted" in str(caught.value)


def test_the_canary_refuses_a_rejection_for_the_wrong_reason(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal that never names the table does not prove the table was found."""
    real = gate._check_objects

    def unreadable(
        sql: str,
        con: duckdb.DuckDBPyConnection,
        allowed: set[str],
        evicted: frozenset[str],
    ) -> None:
        if not allowed:
            raise QueryRejectedError("the SQL could not be analyzed: something new")
        real(sql, con, allowed, evicted)

    monkeypatch.setattr(gate, "_check_objects", unreadable)
    with pytest.raises(GateContractError, match="something new"):
        verify_parser_contract(connection)


def test_the_canary_refuses_when_a_legal_cte_is_refused(
    connection: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that refuses everything would break every session, so name that too."""
    real = gate._check_objects

    def refuse_when_allowed(
        sql: str,
        con: duckdb.DuckDBPyConnection,
        allowed: set[str],
        evicted: frozenset[str],
    ) -> None:
        if allowed:
            raise QueryRejectedError("refused")
        real(sql, con, allowed, evicted)

    monkeypatch.setattr(gate, "_check_objects", refuse_when_allowed)
    with pytest.raises(GateContractError, match="refused"):
        verify_parser_contract(connection)


def test_a_store_that_fails_the_canary_does_not_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_connection: duckdb.DuckDBPyConnection) -> None:
        raise GateContractError("cannot read this DuckDB")

    monkeypatch.setattr("sluice.store.verify_parser_contract", fail)
    with pytest.raises(GateContractError, match="cannot read this DuckDB"):
        Store.open(Limits())


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        (
            "WITH RECURSIVE r(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM r WHERE n < 2000) "
            "SELECT n, pg_get_viewdef(n) FROM r WHERE pg_get_viewdef(n) LIKE '%sluice_calls__%'",
            "pg_get_viewdef",
        ),
        ("SELECT pg_catalog.pg_get_viewdef(1)", "pg_get_viewdef"),
        ("SELECT PG_GET_VIEWDEF(1)", "PG_GET_VIEWDEF"),
        ("SELECT * FROM (SELECT pg_get_viewdef(1) AS v) s", "pg_get_viewdef"),
        ("WITH x AS (SELECT pg_get_viewdef(1)) SELECT * FROM x", "pg_get_viewdef"),
        ("SELECT pg_get_constraintdef(1)", "pg_get_constraintdef"),
        ("SELECT format_type(1, 1)", "format_type"),
        ("SELECT get_block_size('memory')", "get_block_size"),
        ("SELECT pg_sleep(2)", "pg_sleep"),
        ("SELECT generate_subscripts([1, 2, 3], 1)", "generate_subscripts"),
        ("SELECT regexp_split_to_table('a,b', ',')", "regexp_split_to_table"),
    ],
)
def test_builtin_macros_cannot_reach_what_the_gate_denies(
    sql: str, fragment: str, connection: duckdb.DuckDBPyConnection
) -> None:
    connection.execute("CREATE VIEW sluice_calls__0123 AS SELECT * FROM sluice_calls")
    message = _reject(sql, connection)
    assert "function not allowed" in message
    assert fragment.casefold() in message.casefold()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT list_avg([1, 2, 3])",
        "SELECT json_group_array(a) FROM mine",
        "SELECT histogram(a) FROM mine",
        "SELECT if(a > 1, 'big', 'small') FROM mine",
        "SELECT array_to_string(['a', 'b'], ',')",
        "SELECT date_add(DATE '2026-01-01', INTERVAL 1 DAY)",
    ],
)
def test_ordinary_macros_and_aggregates_are_still_admitted(
    sql: str, connection: duckdb.DuckDBPyConnection
) -> None:
    check(sql, connection, ALLOWED)
    connection.execute(sql).fetchall()


def test_the_canary_refuses_when_macros_are_not_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_restricted_macros", lambda _con, restricted: restricted)
    with pytest.raises(GateContractError, match="pg_get_viewdef"):
        verify_parser_contract(duckdb.connect(":memory:"))


def test_the_canary_refuses_when_scalar_denials_go_unseen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS", frozenset())
    with pytest.raises(GateContractError, match="sleep_ms"):
        verify_parser_contract(duckdb.connect(":memory:"))
