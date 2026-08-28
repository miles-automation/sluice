"""Behavior this design rests on, asserted against whatever versions are installed.

`pyproject.toml` sets lower bounds and no upper bounds, so DuckDB and the MCP SDK
float. Almost every load-bearing decision in `spec/001-scratch-db.md` came from
measuring these libraries (see `plan/001-notes-m0.md`), and a minor release could
change any of them while every other test stayed green and the product quietly
became wrong.

This file is the tripwire. When one of these fails after an upgrade, the fix is
to re-read the named spec section, not to relax the assertion.
"""

import statistics
import threading
import time
from pathlib import Path

import duckdb
import pytest
from mcp import Client
from mcp.types.version import LATEST_HANDSHAKE_VERSION, MODERN_PROTOCOL_VERSIONS

from tests.fake_server import build_fake_server

pytestmark = pytest.mark.contract

LOCKDOWN = (
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    "SET lock_configuration = true",
)


def _locked_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for statement in LOCKDOWN:
        con.execute(statement)
    return con


# --------------------------------------------------------------------------
# DuckDB: the query safety layers (spec 6.1)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def readable_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Files that a connection WITHOUT the lockdown can genuinely read.

    Pointing these assertions at `/etc/hosts` made four of them pass vacuously:
    that file is neither valid JSON nor valid Parquet, so the reads failed on
    parsing whether or not external access was closed. A security test that
    passes without the security control is worse than no test.
    """
    directory = tmp_path_factory.mktemp("readable")
    json_path = directory / "rows.json"
    json_path.write_text('[{"a": 1}, {"a": 2}]', encoding="utf-8")
    parquet_path = directory / "rows.parquet"
    open_con = duckdb.connect(":memory:")
    open_con.execute(f"COPY (SELECT 1 AS a) TO '{parquet_path}' (FORMAT PARQUET)")
    open_con.close()
    return {
        "json": str(json_path),
        "parquet": str(parquet_path),
        "glob": str(directory / "*"),
    }


def test_the_fixture_files_really_are_readable_without_the_lockdown(
    readable_files: dict[str, str],
) -> None:
    """Guards the guard. If these ever stop reading cleanly, the blocked-by-
    lockdown assertions below go back to passing for the wrong reason."""
    con = duckdb.connect(":memory:")
    assert con.execute(f"SELECT count(*) FROM read_json('{readable_files['json']}')").fetchall()
    assert con.execute(
        f"SELECT count(*) FROM read_parquet('{readable_files['parquet']}')"
    ).fetchall()
    assert con.execute(f"SELECT count(*) FROM glob('{readable_files['glob']}')").fetchall()


@pytest.mark.parametrize("kind", ["json", "parquet", "glob"])
def test_lockdown_blocks_reads_that_would_otherwise_succeed(
    readable_files: dict[str, str], kind: str
) -> None:
    reader = {"json": "read_json", "parquet": "read_parquet", "glob": "glob"}[kind]
    con = _locked_connection()
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM {reader}('{readable_files[kind]}')").fetchall()


def test_lockdown_blocks_our_own_read_json(readable_files: dict[str, str]) -> None:
    """The finding that forced file-free materialization (spec 5.4).

    `enable_external_access` is database-global: it applies to Sluice's own SQL
    exactly as it applies to the agent's. The file here is valid JSON, so the
    only thing that can stop the read is the lockdown.
    """
    con = _locked_connection()
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_json('{readable_files['json']}')").fetchall()


def test_lockdown_blocks_writing_data_out(tmp_path: Path) -> None:
    con = _locked_connection()
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"COPY (SELECT 1) TO '{tmp_path / 'exfil.csv'}'")


@pytest.mark.parametrize(
    "sql", ["ATTACH '/tmp/sluice_contract.db' AS evil", "INSTALL httpfs", "LOAD httpfs"]
)
def test_lockdown_blocks_attach_and_extensions(sql: str) -> None:
    con = _locked_connection()
    with pytest.raises(duckdb.PermissionException):
        con.execute(sql).fetchall()


def test_configuration_is_frozen_by_the_lock() -> None:
    """`threads` is settable on a fresh connection, so this proves the lock
    itself. `enable_external_access = true` would not: it is refused at runtime
    for an unrelated reason even without the lock."""
    open_con = duckdb.connect(":memory:")
    open_con.execute("SET threads = 3")
    open_con.close()
    con = _locked_connection()
    with pytest.raises(duckdb.Error):
        con.execute("SET threads = 3")


def test_engine_lockdown_does_not_block_pragma() -> None:
    """Layer 2 does not stop PRAGMA. Only the statement gate does, which is why
    spec 6.1 needs all three layers rather than the engine settings alone."""
    con = _locked_connection()
    assert con.execute("PRAGMA database_list").fetchall()


@pytest.mark.parametrize(
    ("sql", "count", "first_type"),
    [
        ("SELECT 1", 1, "SELECT"),
        ("SELECT 1;", 1, "SELECT"),
        ("SELECT 1;\n", 1, "SELECT"),
        ("SELECT 1 -- ; comment", 1, "SELECT"),
        ("WITH x AS (SELECT 1 AS n) SELECT n FROM x", 1, "SELECT"),
        ("SELECT 1; SELECT 2", 2, "SELECT"),
        ("INSERT INTO t VALUES (1)", 1, "INSERT"),
        ("COPY (SELECT 1) TO '/tmp/x.csv'", 1, "COPY"),
    ],
)
def test_extract_statements_classifies(sql: str, count: int, first_type: str) -> None:
    """The layer-1 gate is the engine's own parser, so it needs no preprocessing
    for trailing semicolons, comments, or CTEs."""
    con = duckdb.connect(":memory:")
    statements = con.extract_statements(sql)
    assert len(statements) == count
    assert statements[0].type.name == first_type


# --------------------------------------------------------------------------
# DuckDB: the correctness domain (spec 5.6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [400, 401])
@pytest.mark.parametrize("column_type", ["BIGINT", "DOUBLE"])
def test_median_is_exactly_equal_inside_the_domain(n: int, column_type: str) -> None:
    values: list[float] | list[int]
    values = [i for i in range(n)] if column_type == "BIGINT" else [i * 1.7 for i in range(n)]
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE t (v {column_type})")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    assert con.execute("SELECT median(v) FROM t").fetchall()[0][0] == statistics.median(values)


def test_avg_is_not_exactly_equal_and_needs_a_tolerance() -> None:
    """If this ever starts passing, spec 5.6's tolerance class can shrink. Until
    then, asserting exact equality on `avg` would be a flaky test."""
    values = [i * 1.7 for i in range(400)]
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    duck = con.execute("SELECT avg(v) FROM t").fetchall()[0][0]
    assert duck != statistics.fmean(values)
    assert duck == pytest.approx(statistics.fmean(values), rel=1e-9, abs=1e-12)


def test_relative_tolerance_alone_does_not_survive_cancellation() -> None:
    """Why spec 5.6 states an absolute tolerance as well as a relative one."""
    values = [-1e308, 1.0, 2.0, 1e308]
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    assert con.execute("SELECT avg(v) FROM t").fetchall()[0][0] == 0.0
    assert statistics.fmean(values) == 0.75


def test_median_on_a_json_column_returns_a_lexicographic_answer() -> None:
    """The reason spec 5.5 types mixed scalar columns as VARCHAR.

    DuckDB's own inference makes this column JSON. `sum` and `avg` then raise,
    which is loud and fine, but `median` succeeds and returns a number-shaped
    string that is not the median of anything.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (v JSON)")
    rows = [(str(i),) for i in range(300)] + [('"oops"',)]
    con.executemany("INSERT INTO t VALUES (?)", rows)
    median = con.execute("SELECT median(v) FROM t").fetchall()[0][0]
    assert median == "232"
    assert isinstance(median, str)  # number-shaped, and not a number
    for aggregate in ("sum(v)", "avg(v)"):
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT {aggregate} FROM t").fetchall()


# --------------------------------------------------------------------------
# DuckDB: interrupt scope (spec 6.2)
# --------------------------------------------------------------------------

_LONG_QUERY = "SELECT sum(sin(i)) FROM range(600000000) t(i)"


def _run_and_interrupt(
    target: duckdb.DuckDBPyConnection, executor: duckdb.DuckDBPyConnection
) -> tuple[bool, float]:
    """Run a long query on `executor`, interrupt `target` after a moment."""
    outcome: dict[str, bool] = {}

    def run() -> None:
        try:
            executor.execute(_LONG_QUERY).fetchall()
            outcome["interrupted"] = False
        except duckdb.InterruptException:
            outcome["interrupted"] = True

    started = time.monotonic()
    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.3)
    target.interrupt()
    worker.join(timeout=60)
    assert not worker.is_alive(), "the query thread never terminated"
    assert "interrupted" in outcome, "the query neither finished nor raised"
    return outcome["interrupted"], time.monotonic() - started


@pytest.mark.slow
def test_interrupt_reaches_the_executing_connection() -> None:
    con = duckdb.connect(":memory:")
    cursor = con.cursor()
    interrupted, elapsed = _run_and_interrupt(target=cursor, executor=cursor)
    assert interrupted
    assert elapsed < 2.0


@pytest.mark.slow
def test_interrupt_on_the_parent_does_not_reach_a_cursor() -> None:
    """The watchdog must hold the exact connection object the query runs on.

    A timeout implemented against the parent silently does nothing and looks
    like it works, because short queries finish on their own.
    """
    con = duckdb.connect(":memory:")
    cursor = con.cursor()
    interrupted, _ = _run_and_interrupt(target=con, executor=cursor)
    assert not interrupted


# --------------------------------------------------------------------------
# MCP SDK (spec 2, 8, 11)
# --------------------------------------------------------------------------


def test_round_trips_require_the_modern_protocol_version() -> None:
    """`InputRequiredResult` is only in the `tools/call` result union at
    2026-07-28, and the initialize handshake cannot reach that version."""
    from mcp_types.methods import SERVER_RESULTS

    modern = SERVER_RESULTS[("tools/call", "2026-07-28")]
    assert "InputRequiredResult" in str(modern)
    legacy = SERVER_RESULTS[("tools/call", LATEST_HANDSHAKE_VERSION)]
    assert "InputRequiredResult" not in str(legacy)
    assert LATEST_HANDSHAKE_VERSION not in MODERN_PROTOCOL_VERSIONS


@pytest.mark.anyio
async def test_client_discover_negotiates_the_modern_version() -> None:
    """Why the proxy connects with `Client` rather than a bare ClientSession."""
    async with Client(build_fake_server()) as client:
        assert client.protocol_version in MODERN_PROTOCOL_VERSIONS


@pytest.mark.anyio
async def test_output_schema_violation_raises_with_a_classifiable_message() -> None:
    """`proxy.py` classifies this failure by message substring because the SDK
    raises a bare RuntimeError. This pins the substrings it matches on."""
    async with Client(build_fake_server()) as client:
        with pytest.raises(RuntimeError) as caught:
            await client.session.call_tool("bad_schema", None, allow_input_required=True)
    message = str(caught.value).lower()
    assert "output schema" in message or "structured content" in message
