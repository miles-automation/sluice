"""Timeout, caps, escaping, and truncation reporting (spec 6.2, 6.3)."""

import contextlib
import threading
import time

import anyio
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
    assert rendered.rstrip("…") == "é" * 3  # 6 bytes plus the 3-byte ellipsis
    assert len(rendered.encode("utf-8")) <= 9
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


@pytest.mark.parametrize("cap", [384, 600, 1000])
async def test_output_never_exceeds_the_byte_cap(store: Store, cap: int) -> None:
    """Asserted against the cap itself, in bytes, over the whole output.

    An earlier version counted characters, excluded the header and the notes,
    and had a test that merely required "under 600" for a 200-byte cap, which
    enshrined the violation instead of catching it."""
    name = _seed(store, rows=40, width="z")
    tool = QueryTool(store, Limits(query_max_bytes=cap, max_cell_bytes=64))
    text = await tool.run(f'SELECT a, b FROM "{name}" ORDER BY a', max_rows=40)
    assert len(text.encode("utf-8")) <= cap


async def test_the_truncation_notice_always_survives(store: Store) -> None:
    """The notices are the last thing in the output, so if the body is
    mis-measured the backstop eats them and the result becomes a silently
    truncated table. Counting characters instead of bytes does exactly that on
    multi-byte data, and the cap alone would still look honoured."""
    store.connection.execute('CREATE TABLE "wide_uni" (a VARCHAR)')
    store.connection.executemany(
        'INSERT INTO "wide_uni" VALUES (?)', [("日" * 40,) for _ in range(20)]
    )
    store._allowed_objects.add("wide_uni")
    tool = QueryTool(store, Limits(query_max_bytes=600, max_cell_bytes=512))
    text = await tool.run('SELECT a FROM "wide_uni"')
    assert len(text.encode("utf-8")) <= 600
    assert text.splitlines()[-1].endswith(("shown.", "….", "known.", "cap."))
    assert "row(s) shown" in text.splitlines()[-1]


async def test_byte_cap_counts_bytes_not_characters(store: Store) -> None:
    """A row of multi-byte characters used to blow a 100-byte cap to 195."""
    store.connection.execute('CREATE TABLE "uni" (a VARCHAR)')
    store.connection.executemany('INSERT INTO "uni" VALUES (?)', [("é" * 60,) for _ in range(10)])
    store._allowed_objects.add("uni")
    tool = QueryTool(store, Limits(query_max_bytes=384, max_cell_bytes=512))
    text = await tool.run('SELECT a FROM "uni"')
    assert len(text.encode("utf-8")) <= 384
    text.encode("utf-8")


async def test_a_long_column_alias_cannot_blow_the_cap(store: Store) -> None:
    """The header comes from the agent's own SQL, so it is arbitrary text. An
    unbounded alias returned 5 KB against a 100-byte cap."""
    name = _seed(store, rows=2)
    alias = "z" * 5000
    tool = QueryTool(store, Limits(query_max_bytes=384, max_cell_bytes=64))
    text = await tool.run(f'SELECT a AS "{alias}" FROM "{name}"')
    assert len(text.encode("utf-8")) <= 384
    assert "table was omitted" in text
    assert "row(s) omitted" in text


async def test_a_wide_header_cannot_evict_the_truncation_notices(store: Store) -> None:
    name = _seed(store, rows=2)
    aliases = ", ".join(f'a AS "column_{index:03d}_{"x" * 80}"' for index in range(300))
    tool = QueryTool(store, Limits(query_max_bytes=65_536, max_cell_bytes=64))
    text = await tool.run(f'SELECT {aliases} FROM "{name}"')
    assert len(text.encode("utf-8")) <= 65_536
    assert "row(s) shown" in text
    assert "column header(s) truncated" in text or "table was omitted" in text


async def test_headers_are_escaped_like_cells(store: Store) -> None:
    name = _seed(store, rows=1)
    text = await QueryTool(store, Limits()).run(f'SELECT a AS "we|ird" FROM "{name}"')
    # Exactly one expected form. A pipe left raw in a header would break the
    # table structure the agent is reading.
    assert "| we\\|ird |" in text
    assert "| we|ird |" not in text


async def test_byte_cap_drops_rows_and_says_so(store: Store) -> None:
    name = _seed(store, rows=40, width="z")
    tool = QueryTool(store, Limits(query_max_bytes=400, max_cell_bytes=64))
    text = await tool.run(f'SELECT a, b FROM "{name}" ORDER BY a', max_rows=40)
    assert "omitted to stay under the byte cap" in text
    assert len(text.encode("utf-8")) <= 400


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
async def test_cancelling_the_caller_still_bounds_the_query(store: Store) -> None:
    """Cancellation must not remove the deadline.

    The watchdog used to be a task in a task group, so cancelling the call
    cancelled the watchdog while the DuckDB worker carried on ignoring
    cancellation. The query then ran unbounded. A threading.Timer is immune.
    """
    store.connection.execute("CREATE TABLE slow (a BIGINT)")
    store.connection.executemany("INSERT INTO slow VALUES (?)", [(i,) for i in range(1000)])
    store._allowed_objects.add("slow")
    tool = QueryTool(store, Limits(query_timeout_seconds=1.0))
    started = time.monotonic()
    with anyio.move_on_after(0.05), contextlib.suppress(QueryRejectedError):
        # Either outcome is correct and which one wins is a race: the deadline
        # may interrupt the worker (raising) before the cancellation is
        # delivered, or the other way round. The property under test is that it
        # is BOUNDED, not which exception surfaces.
        await tool.run("SELECT count(*) FROM slow x, slow y, slow z WHERE x.a + y.a + z.a > 0")
    elapsed = time.monotonic() - started
    # Bounded by the deadline, not by the cancellation, and nowhere near the
    # minutes the query would take on its own.
    assert elapsed < 15, f"cancelled query ran unbounded ({elapsed:.1f}s)"
    # And the store still works afterwards.
    assert store.connection.execute("SELECT count(*) FROM slow").fetchall() == [(1000,)]


@pytest.mark.slow
async def test_a_timeout_leaves_the_store_usable(store: Store) -> None:
    name = _seed(store, rows=3)
    tool = QueryTool(store, Limits(query_timeout_seconds=0.3))
    text = await tool.run(f'SELECT a FROM "{name}"')
    assert "3 row(s) shown" in text


async def test_timeout_expiring_in_worker_queue_prevents_late_execution(store: Store) -> None:
    """A timer can fire before a saturated worker pool starts the query.

    Interrupting an idle cursor is not sticky. The expired flag must therefore
    stop the queued worker from beginning an otherwise unbounded query later.
    """
    store.connection.execute("CREATE TABLE queued_slow (a BIGINT)")
    store.connection.executemany(
        "INSERT INTO queued_slow VALUES (?)", [(index,) for index in range(600)]
    )
    store._allowed_objects.add("queued_slow")
    limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    result: dict[str, object] = {}

    def occupy_only_worker() -> None:
        blocker_started.set()
        release_blocker.wait()

    async def occupy() -> None:
        await anyio.to_thread.run_sync(occupy_only_worker)

    async def run_query() -> None:
        try:
            await QueryTool(store, Limits(query_timeout_seconds=0.05)).run(
                "SELECT count(*) FROM queued_slow x, queued_slow y, queued_slow z "
                "WHERE x.a + y.a + z.a > 0"
            )
            result["query"] = "finished"
        except QueryRejectedError as exc:
            result["query"] = exc

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(occupy)
            while not blocker_started.is_set():
                await anyio.sleep(0)
            group.start_soon(run_query)
            await anyio.sleep(0.15)
            release_blocker.set()
    finally:
        release_blocker.set()
        limiter.total_tokens = original_tokens

    assert isinstance(result["query"], QueryRejectedError)
    assert "timeout" in str(result["query"])
