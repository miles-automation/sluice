"""Isolation between queries, and between a query and a write (spec 6.2, 9)."""

import anyio
import pytest

from sluice.config import Limits
from sluice.gate import QueryRejectedError
from sluice.query import QueryTool
from sluice.store import Store

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

SLOW_SQL = "SELECT count(*) FROM slow x, slow y, slow z WHERE x.a + y.a + z.a > 0"


def _seed_slow(store: Store) -> None:
    store.connection.execute("CREATE TABLE slow (a BIGINT)")
    store.connection.executemany("INSERT INTO slow VALUES (?)", [(i,) for i in range(1000)])
    store.connection.execute("CREATE TABLE quick (a BIGINT)")
    store.connection.executemany("INSERT INTO quick VALUES (?)", [(1,), (2,), (3,)])
    store._allowed_objects.update({"slow", "quick"})


async def test_a_timing_out_query_does_not_disturb_a_concurrent_query(store: Store) -> None:
    """Each query runs on its own connection, so one timeout must not abort the
    other. A shared query connection would let either one's watchdog kill both."""
    _seed_slow(store)
    slow_tool = QueryTool(store, Limits(query_timeout_seconds=0.5))
    fast_tool = QueryTool(store, Limits(query_timeout_seconds=30))
    results: dict[str, object] = {}

    async def run_slow() -> None:
        try:
            await slow_tool.run(SLOW_SQL)
            results["slow"] = "finished"
        except QueryRejectedError as exc:
            results["slow"] = f"rejected: {exc}"

    async def run_fast() -> None:
        await anyio.sleep(0.1)
        results["fast"] = await fast_tool.run("SELECT sum(a) FROM quick")

    async with anyio.create_task_group() as group:
        group.start_soon(run_slow)
        group.start_soon(run_fast)

    assert "timeout" in str(results["slow"])
    assert "1 row(s) shown" in str(results["fast"])
    assert "| 6 |" in str(results["fast"])


async def test_a_timing_out_query_does_not_disturb_a_concurrent_write(store: Store) -> None:
    """The invariant this protects: Sluice never turns a working tool call into
    a failed one. A connection-wide interrupt would break it from the inside."""
    _seed_slow(store)
    tool = QueryTool(store, Limits(query_timeout_seconds=0.5))
    outcome: dict[str, object] = {}

    async def run_query() -> None:
        try:
            await tool.run(SLOW_SQL)
            outcome["query"] = "finished"
        except QueryRejectedError as exc:
            outcome["query"] = str(exc)

    async def run_write() -> None:
        await anyio.sleep(0.1)
        store.connection.execute("CREATE TABLE written AS SELECT 42 AS a")
        outcome["write"] = store.connection.execute("SELECT a FROM written").fetchall()

    async with anyio.create_task_group() as group:
        group.start_soon(run_query)
        group.start_soon(run_write)

    assert "timeout" in str(outcome["query"])
    assert outcome["write"] == [(42,)]
    # And the store is still usable afterwards.
    assert store.connection.execute("SELECT sum(a) FROM quick").fetchall() == [(6,)]


async def test_two_concurrent_queries_both_return(store: Store) -> None:
    _seed_slow(store)
    tool = QueryTool(store, Limits())
    async with anyio.create_task_group() as group:
        collected: list[str] = []

        async def run(sql: str) -> None:
            collected.append(await tool.run(sql))

        for _ in range(4):
            group.start_soon(run, "SELECT sum(a) FROM quick")
    assert len(collected) == 4
    assert all("| 6 |" in text for text in collected)
