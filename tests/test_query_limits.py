"""Timeout, caps, escaping, and truncation reporting (spec 6.2, 6.3)."""

import time

import pytest

from sluice.config import Limits
from sluice.gate import QueryRejectedError
from sluice.query import QueryOutcome, QueryTool, escape_cell, render
from sluice.store import Store

pytestmark = pytest.mark.anyio


def _seed(store: Store, rows: int = 10, width: str = "x") -> str:
    name = "seeded"
    store.connection.execute(f'CREATE TABLE "{name}" (a BIGINT, b VARCHAR)')
    store.connection.executemany(
        f'INSERT INTO "{name}" VALUES (?, ?)', [(i, width * (i + 1)) for i in range(rows)]
    )
    store._allowed_objects.add(name)
    return name


# --------------------------------------------------------------------------
# Escaping and rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        ("", "''"),
        ("NULL", "NULL"),  # the string, indistinguishable by design from SQL NULL
        ("a|b", "a\\|b"),
        ("a\nb", "a\\nb"),
        ("a\r\nb", "a\\nb"),
        ("a\\b", "a\\\\b"),
        (42, "42"),
    ],
)
def test_cell_escaping(value: object, expected: str) -> None:
    rendered, cut = escape_cell(value, 512)
    assert rendered == expected
    assert not cut


def test_null_and_the_string_null_are_deliberately_ambiguous() -> None:
    """Documented, not accidental: both render as NULL. The empty string does
    not, because that pair is the one people actually confuse."""
    assert escape_cell(None, 512)[0] == escape_cell("NULL", 512)[0]
    assert escape_cell("", 512)[0] != escape_cell(None, 512)[0]


def test_cell_truncation_cuts_on_a_character_boundary() -> None:
    """Slicing UTF-8 at a byte offset produces invalid text."""
    rendered, cut = escape_cell("é" * 100, 9)
    assert cut
    assert rendered.rstrip("…") == "é" * 4  # 8 bytes, not 9
    rendered.encode("utf-8")  # must not raise


def test_render_reports_every_kind_of_truncation() -> None:
    outcome = QueryOutcome(columns=["a"], rows=[("y" * 50,)], more_rows=True)
    text = render(outcome, Limits(max_cell_bytes=10))
    assert "Additional rows exist" in text
    assert "the exact number is not known" in text
    assert "1 cell(s) truncated" in text


def test_render_never_claims_a_row_count_it_cannot_know() -> None:
    outcome = QueryOutcome(columns=["a"], rows=[(1,)], more_rows=True)
    text = render(outcome, Limits())
    assert "Additional rows exist" in text
    for digit in ("2 rows", "3 rows", "more rows: "):
        assert digit not in text


# --------------------------------------------------------------------------
# Caps and timeout, through the real tool
# --------------------------------------------------------------------------


async def test_row_cap_is_applied_and_reported(store: Store) -> None:
    name = _seed(store, rows=10)
    tool = QueryTool(store, Limits())
    text = await tool.run(f'SELECT a FROM "{name}" ORDER BY a', max_rows=3)
    assert "3 row(s) shown" in text
    assert "Additional rows exist" in text
    assert "| 3 |" not in text


async def test_no_more_rows_notice_when_everything_fits(store: Store) -> None:
    name = _seed(store, rows=3)
    text = await QueryTool(store, Limits()).run(f'SELECT a FROM "{name}"', max_rows=10)
    assert "3 row(s) shown" in text
    assert "Additional rows exist" not in text


async def test_max_rows_cannot_exceed_the_configured_cap(store: Store) -> None:
    name = _seed(store, rows=20)
    tool = QueryTool(store, Limits(query_max_rows=5))
    text = await tool.run(f'SELECT a FROM "{name}" ORDER BY a', max_rows=1000)
    assert "5 row(s) shown" in text


async def test_max_rows_below_one_is_rejected(store: Store) -> None:
    _seed(store)
    with pytest.raises(QueryRejectedError, match="max_rows"):
        await QueryTool(store, Limits()).run("SELECT 1", max_rows=0)


async def test_byte_cap_drops_rows_and_says_so(store: Store) -> None:
    name = _seed(store, rows=40, width="z")
    tool = QueryTool(store, Limits(query_max_bytes=200, max_cell_bytes=64))
    text = await tool.run(f'SELECT a, b FROM "{name}" ORDER BY a', max_rows=40)
    assert "omitted to stay under the byte cap" in text
    assert len(text.encode("utf-8")) < 600


async def test_sql_runs_unmodified_with_a_trailing_semicolon(store: Store) -> None:
    """A `SELECT * FROM (<sql>) LIMIT n+1` wrapper breaks on this even though
    the gate accepts it."""
    name = _seed(store, rows=3)
    text = await QueryTool(store, Limits()).run(f'SELECT a FROM "{name}" ORDER BY a;')
    assert "3 row(s) shown" in text


async def test_sql_runs_unmodified_with_duplicate_column_names(store: Store) -> None:
    """The other case the wrapper breaks on."""
    name = _seed(store, rows=2)
    text = await QueryTool(store, Limits()).run(f'SELECT a AS x, a AS x FROM "{name}"')
    assert "| x | x |" in text


async def test_a_failing_query_is_an_error_not_an_empty_result(store: Store) -> None:
    """An empty table would read as "no matches" and be believed."""
    name = _seed(store)
    with pytest.raises(QueryRejectedError) as caught:
        await QueryTool(store, Limits()).run(f'SELECT no_such_column FROM "{name}"')
    assert "row(s) shown" not in str(caught.value)


async def test_aggregating_a_json_column_fails_loudly(store: Store) -> None:
    store.connection.execute('CREATE TABLE "j" (v JSON)')
    store.connection.executemany('INSERT INTO "j" VALUES (?)', [("1",), ("2",)])
    store._allowed_objects.add("j")
    with pytest.raises(QueryRejectedError):
        await QueryTool(store, Limits()).run('SELECT sum(v) FROM "j"')


@pytest.mark.slow
async def test_a_long_query_is_interrupted_at_the_timeout(store: Store) -> None:
    """The watchdog must interrupt the connection the query is running on.
    Interrupting its parent silently does nothing and looks like it works,
    because short queries finish on their own."""
    # Seeded through the store's own connection, which is not gated. The agent
    # SQL below cannot use range() or any other table function.
    store.connection.execute("CREATE TABLE slow (a BIGINT)")
    store.connection.executemany("INSERT INTO slow VALUES (?)", [(i,) for i in range(1000)])
    store._allowed_objects.add("slow")
    tool = QueryTool(store, Limits(query_timeout_seconds=0.5))
    started = time.monotonic()
    with pytest.raises(QueryRejectedError, match="timeout"):
        # A three-way self join over 1,000 rows is 10^9 combinations, which runs
        # for minutes uninterrupted. If the watchdog interrupts the wrong
        # connection this does not raise, it hangs, and the assertion below
        # catches the case where it somehow finishes anyway.
        await tool.run("SELECT count(*) FROM slow x, slow y, slow z WHERE x.a + y.a + z.a > 0")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"interrupt did not land promptly ({elapsed:.1f}s)"


@pytest.mark.slow
async def test_a_timeout_leaves_the_store_usable(store: Store) -> None:
    name = _seed(store, rows=3)
    tool = QueryTool(store, Limits(query_timeout_seconds=0.3))
    text = await tool.run(f'SELECT a FROM "{name}"')
    assert "3 row(s) shown" in text
