"""The three-layer gate (spec 6.1).

Two rules for this file. Every rejection is asserted with the specific reason,
so a case cannot pass because the parser choked on it for an unrelated cause.
And every rejection has a matching acceptance nearby, because a gate that
refuses legal SQL is its own defect.
"""

import duckdb
import pytest

from sluice.gate import QueryRejectedError, check

pytestmark = pytest.mark.anyio


@pytest.fixture
def connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE mine (a BIGINT, b VARCHAR)")
    con.execute("CREATE TABLE sluice_calls (scope_id VARCHAR)")
    con.execute("INSERT INTO mine VALUES (1, 'x'), (2, 'y')")
    return con


ALLOWED = {"mine"}


def _reject(sql: str, connection: duckdb.DuckDBPyConnection) -> str:
    with pytest.raises(QueryRejectedError) as caught:
        check(sql, connection, ALLOWED)
    return str(caught.value)


# --------------------------------------------------------------------------
# Layer 1: one statement, SELECT only
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO mine VALUES (3, 'z')",
        "UPDATE mine SET a = 9",
        "DELETE FROM mine",
        "CREATE TABLE evil AS SELECT 1",
        "DROP TABLE mine",
        "ATTACH '/tmp/x.db' AS x",
        "COPY (SELECT 1) TO '/tmp/out.csv'",
        "SET enable_external_access = true",
        "CALL duckdb_tables()",
    ],
)
def test_non_select_statements_are_rejected(
    sql: str, connection: duckdb.DuckDBPyConnection
) -> None:
    assert "only SELECT" in _reject(sql, connection)


def test_multiple_statements_are_rejected_by_count(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Caught by counting parsed statements, not by splitting on semicolons."""
    assert "2 statements" in _reject("SELECT 1; DROP TABLE mine", connection)


def test_a_semicolon_inside_a_string_is_not_two_statements(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    check("SELECT ';' AS semi", connection, ALLOWED)


# --------------------------------------------------------------------------
# Layer 3: only objects Sluice created
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT * FROM duckdb_tables()", "table function"),
        ("SELECT * FROM duckdb_columns()", "table function"),
        ("SELECT * FROM duckdb_views()", "table function"),
        ("SELECT * FROM read_csv('/etc/hosts')", "table function"),
        ("SELECT * FROM read_json('/etc/hosts')", "table function"),
        ("SELECT * FROM glob('/etc/*')", "table function"),
        ("SELECT * FROM range(10)", "table function"),
        ("SELECT unnest(range(1000000000))", "function not allowed"),
        ("SELECT write_log('agent-controlled')", "function not allowed"),
        ("SELECT lpad('x', 1000000000, 'x')", "function not allowed"),
        ("SELECT list_resize([1], 1000000000)", "function not allowed"),
        ("SELECT bitstring('1', 1000000000)", "function not allowed"),
        ("SELECT * FROM information_schema.tables", "schema-qualified"),
        ("SELECT * FROM pg_catalog.pg_tables", "schema-qualified"),
        ("SELECT * FROM sqlite_master", "unknown table"),
        ("SELECT * FROM sluice_calls", "unknown table"),
        ("SELECT * FROM mine JOIN duckdb_tables() ON 1=1", "table function"),
        ("SELECT (SELECT count(*) FROM duckdb_tables())", "table function"),
        ("WITH x AS (SELECT * FROM duckdb_tables()) SELECT * FROM x", "table function"),
        ("SELECT * FROM (SELECT * FROM sqlite_master)", "unknown table"),
    ],
)
def test_enumeration_and_filesystem_vectors_are_rejected(
    sql: str, reason: str, connection: duckdb.DuckDBPyConnection
) -> None:
    assert reason in _reject(sql, connection)


@pytest.mark.parametrize(
    "sql",
    ["SHOW TABLES", "SHOW ALL TABLES", "DESCRIBE mine", "SUMMARIZE mine"],
)
def test_show_and_describe_are_rejected(sql: str, connection: duckdb.DuckDBPyConnection) -> None:
    """Layer 1 does NOT catch these: `extract_statements` types every one of
    them as SELECT on DuckDB 1.5.5. Only the object check stops them."""
    assert "SHOW and DESCRIBE" in _reject(sql, connection)


@pytest.mark.parametrize(
    "sql",
    ["PRAGMA show_tables", "PRAGMA database_list", "PRAGMA table_info('mine')"],
)
def test_pragmas_are_rejected(sql: str, connection: duckdb.DuckDBPyConnection) -> None:
    """Also typed SELECT by the statement gate. They fail closed because they
    cannot be serialized to an AST, which removes the whole family without
    naming any of them."""
    assert "could not be analyzed" in _reject(sql, connection)


def test_the_statement_gate_alone_would_pass_these(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Pins the reason layer 3 exists. If DuckDB ever reclassifies these away
    from SELECT, this fails and the comment above can be revised."""
    for sql in ["SHOW TABLES", "DESCRIBE mine", "PRAGMA show_tables"]:
        assert [s.type.name for s in connection.extract_statements(sql)] == ["SELECT"]


def test_an_unknown_table_names_no_others(connection: duckdb.DuckDBPyConnection) -> None:
    """The rejection must not become an enumeration oracle by listing what does
    exist."""
    message = _reject("SELECT * FROM nope", connection)
    assert "mine" not in message
    assert "sluice_calls" not in message


# --------------------------------------------------------------------------
# Legal SQL must still run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "SELECT 1;\n",
        "SELECT 1 -- ; a comment",
        "SELECT /* ; */ 1",
        "SELECT * FROM mine",
        "FROM mine",
        "TABLE mine",
        "SELECT a, count(*) FROM mine GROUP BY a",
        "SELECT a FROM mine ORDER BY a DESC LIMIT 1",
        "WITH x AS (SELECT a FROM mine) SELECT sum(a) FROM x",
        "WITH x AS (SELECT 1 AS n), y AS (SELECT n FROM x) SELECT * FROM y",
        "SELECT json_extract('{\"a\": 1}', '$.a')",
        "SELECT m1.a FROM mine m1 JOIN mine m2 ON m1.a = m2.a",
        "SELECT * FROM mine UNION ALL SELECT * FROM mine",
        "SELECT a AS a, b AS a FROM mine",
    ],
)
def test_legal_queries_pass(sql: str, connection: duckdb.DuckDBPyConnection) -> None:
    check(sql, connection, ALLOWED)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH sluice_calls AS (SELECT * FROM sluice_calls) SELECT * FROM sluice_calls",
        "WITH x AS (SELECT * FROM sluice_calls), sluice_calls AS (SELECT 1) SELECT * FROM x",
        "WITH RECURSIVE sluice_calls AS (SELECT * FROM sluice_calls "
        "UNION ALL SELECT * FROM sluice_calls) SELECT * FROM sluice_calls",
    ],
)
def test_cte_binding_order_cannot_shadow_physical_tables(
    sql: str, connection: duckdb.DuckDBPyConnection
) -> None:
    """A CTE is not visible in its seed or before its declaration.

    DuckDB resolves those references to the physical table. The gate must use
    the same binding order, rather than treating every CTE name in the query as
    a global allowlist entry.
    """
    assert "unknown table" in _reject(sql, connection)


def test_ordered_ctes_and_recursive_terms_remain_legal(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    check(
        "WITH x AS (SELECT a FROM mine), y AS (SELECT * FROM x) SELECT * FROM y",
        connection,
        ALLOWED,
    )
    check("WITH X AS (SELECT 1 AS n) SELECT * FROM x", connection, ALLOWED)
    check(
        "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL "
        "SELECT n + 1 FROM nums WHERE n < 3) SELECT * FROM nums",
        connection,
        ALLOWED,
    )


def test_unparseable_sql_is_rejected_not_executed(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    assert "could not parse" in _reject("SELECT FROM WHERE", connection)


def test_empty_sql_is_rejected(connection: duckdb.DuckDBPyConnection) -> None:
    assert "no statement" in _reject("   ", connection)
