"""Regression tests for interception admission and session retention."""

import asyncio
import json
from datetime import UTC, datetime

import anyio
import pytest
from mcp import types

from sluice import naming
from sluice import payload as payload_module
from sluice.config import Limits
from sluice.intercept import Interceptor
from sluice.store import ENVELOPE_TABLE, Store

pytestmark = pytest.mark.anyio


def _result(value: dict[str, object], *, dual: bool = False) -> types.CallToolResult:
    text = json.dumps(value)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=value if dual else None,
    )


async def _intercept(
    interceptor: Interceptor, mounted: str, value: dict[str, object], *, dual: bool = False
) -> types.CallToolResult:
    return await interceptor.intercept(
        server="fake",
        tool="rows",
        mounted=mounted,
        arguments=None,
        result=_result(value, dual=dual),
        meta=None,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )


async def test_admission_covers_selection_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = Limits(max_concurrent_materializations=1, max_session_bytes=10_000_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        selected = 0
        commit_started = anyio.Event()
        release_commit = anyio.Event()
        original_select = payload_module.select
        original_commit = store.commit_call

        def counting_select(result: types.CallToolResult):  # type: ignore[no-untyped-def]
            nonlocal selected
            selected += 1
            return original_select(result)

        async def delayed_commit(record, plans):  # type: ignore[no-untyped-def]
            commit_started.set()
            await release_commit.wait()
            return await original_commit(record, plans)

        monkeypatch.setattr(payload_module, "select", counting_select)
        monkeypatch.setattr(store, "commit_call", delayed_commit)
        first = asyncio.create_task(_intercept(interceptor, "fake__rows", {"items": [1]}))
        await commit_started.wait()
        second = asyncio.create_task(_intercept(interceptor, "fake__rows", {"items": [2]}))
        await anyio.sleep(0.02)
        assert selected == 1
        release_commit.set()
        await asyncio.gather(first, second)


async def test_dual_channel_retention_accounts_for_both_channels() -> None:
    limits = Limits(max_session_bytes=10_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        value = {"items": [{"id": 1, "label": "dual"}]}
        await _intercept(interceptor, naming.mounted_name("fake", "rows"), value, dual=True)
        row = store.connection.execute(
            f"SELECT result, result_text, result_blocks, result_structured FROM {ENVELOPE_TABLE}"
        ).fetchone()
        assert row is not None
        assert all(part is not None for part in row)
        assert store.retained_bytes > len(json.dumps(value).encode())


async def test_sequential_retention_evicts_oldest_call_coherently() -> None:
    limits = Limits(max_session_bytes=600)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        mounted = naming.mounted_name("fake", "rows")
        first = await _intercept(interceptor, mounted, {"items": [{"id": 1, "padding": "x" * 100}]})
        second = await _intercept(
            interceptor, mounted, {"items": [{"id": 2, "padding": "y" * 100}]}
        )
        assert first.structured_content is not None
        assert second.structured_content is not None
        first_table = first.structured_content["tables"][0]["name"]
        second_table = second.structured_content["tables"][0]["name"]
        assert first_table not in store.allowed_objects
        assert second_table in store.allowed_objects
        with pytest.raises(Exception, match="does not exist"):
            store.connection.execute(f'SELECT * FROM "{first_table}"').fetchall()
        rows = store.connection.execute(
            f"SELECT tool, flat_tables, flat_reason, result IS NULL FROM {ENVELOPE_TABLE} "
            "ORDER BY seq"
        ).fetchall()
        assert rows[0][0] == "rows"
        assert rows[0][1] == []
        assert rows[0][2] == "retention_evicted"
        assert rows[0][3] is True
        assert rows[1][1] == [second_table]


async def test_call_larger_than_retention_budget_degrades_safely() -> None:
    limits = Limits(max_session_bytes=100)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        result = await _intercept(
            interceptor,
            naming.mounted_name("fake", "rows"),
            {"items": [{"id": 1, "padding": "x" * 100}]},
        )
        assert result.structured_content is not None
        assert result.structured_content["tables"] == []
        assert result.structured_content["flat_reason"] == "retention_budget_exceeded"
        assert store.connection.execute(
            f"SELECT flat_tables, flat_reason, result IS NULL FROM {ENVELOPE_TABLE}"
        ).fetchall() == [([], "retention_budget_exceeded", True)]
