"""Regression tests for interception admission and session retention."""

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime

import anyio
import pytest
from mcp import types

from sluice import naming
from sluice import payload as payload_module
from sluice.config import Limits
from sluice.gate import QueryRejectedError
from sluice.intercept import Interceptor
from sluice.query import QueryTool
from sluice.store import ENVELOPE_TABLE, Store

pytestmark = pytest.mark.anyio


async def _intercept(
    interceptor: Interceptor,
    mounted: str,
    value: Mapping[str, object],
    *,
    mode: str = "text",
    meta: object | None = None,
) -> types.CallToolResult:
    payload = dict(value)
    text = json.dumps(payload) if mode in ("text", "dual") else "summary"
    return await interceptor.intercept(
        server="fake",
        tool="rows",
        mounted=mounted,
        arguments={"filter": "a|b"},
        result=types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=payload if mode in ("structured", "dual") else None,
        ),
        meta=meta,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )


async def test_admission_covers_selection_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = Limits(
        max_concurrent_materializations=1,
        max_payload_bytes=1_000,
        max_session_bytes=10_000_000,
    )
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


async def test_selector_exception_passes_through_and_releases_fifo_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=10_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        original_select = payload_module.select
        calls = 0

        def fail_once(result: types.CallToolResult) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RecursionError("synthetic deeply nested payload")
            return original_select(result)

        monkeypatch.setattr(payload_module, "select", fail_once)
        first = await _intercept(interceptor, "fake__rows", {"items": [{"id": 1}]})
        assert first.structured_content is None
        block = first.content[0]
        assert isinstance(block, types.TextContent)
        assert json.loads(block.text) == {"items": [{"id": 1}]}
        failure = store.connection.execute(
            f"SELECT flat_reason FROM {ENVELOPE_TABLE} WHERE seq = 1"
        ).fetchone()
        assert failure is not None
        assert failure[0].startswith("selection_failed: RecursionError")
        with anyio.fail_after(1):
            result = await _intercept(interceptor, "fake__rows", {"items": [{"id": 2}]})
        assert result.structured_content is not None
        assert result.structured_content["tables"]


async def test_cancelled_planning_releases_fifo_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=10_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        original_build = interceptor._build_plans
        started = anyio.Event()

        async def block_first(mounted, scope_id, selected):  # type: ignore[no-untyped-def]
            if selected.value["items"][0]["id"] == 1:
                started.set()
                await anyio.sleep_forever()
            return await original_build(mounted, scope_id, selected)

        monkeypatch.setattr(interceptor, "_build_plans", block_first)
        cancelled = asyncio.create_task(
            _intercept(interceptor, "fake__rows", {"items": [{"id": 1}]})
        )
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        with anyio.fail_after(1):
            result = await _intercept(interceptor, "fake__rows", {"items": [{"id": 2}]})
        assert result.structured_content is not None
        assert result.structured_content["tables"]


async def test_degraded_commit_retry_resolves_ticket_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = Limits(max_payload_bytes=500, max_session_bytes=500)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        original_commit = store._commit
        calls = 0

        def fail_first(record, plans):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("synthetic write failure")
            return original_commit(record, plans)

        monkeypatch.setattr(store, "_commit", fail_first)
        first = await _intercept(
            interceptor,
            "fake__rows",
            {"items": [{"id": 1, "padding": "x" * 100}]},
        )
        assert first.structured_content is not None
        expected = (
            "retention_budget_exceeded; prior=load_failed: RuntimeError: synthetic write failure"
        )
        assert first.structured_content["flat_reason"] == expected
        row = store.connection.execute(
            f"SELECT flat_reason FROM {ENVELOPE_TABLE} WHERE seq = 1"
        ).fetchone()
        assert row == (expected,)
        with anyio.fail_after(1):
            second = await _intercept(interceptor, "fake__rows", {"items": [{"id": 2}]})
        assert second.structured_content is not None


async def test_overbudget_passthrough_metadata_is_bounded() -> None:
    limits = Limits(max_payload_bytes=500, max_session_bytes=500)
    content: list[types.ContentBlock] = [
        types.ImageContent(type="image", data="AA==", mime_type="image/png") for _ in range(100)
    ]
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        result = types.CallToolResult(content=content)
        returned = await interceptor.intercept(
            server="fake",
            tool="images",
            mounted="fake__images",
            arguments={"untrusted": "x" * 1_000},
            result=result,
            meta=None,
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        assert returned is result
        row = store.connection.execute(
            f"SELECT args, content_kinds, result, result_text, result_blocks, "
            f"result_structured FROM {ENVELOPE_TABLE}"
        ).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] == [*["image"] * payload_module.MAX_CONTENT_KINDS, "truncated"]
        assert row[2:] == (None, None, None, None)


async def test_row_preview_never_switches_to_a_later_table() -> None:
    limits = Limits(
        max_payload_bytes=5_000,
        max_session_bytes=100_000,
        preview_bytes=64,
    )
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        result = await _intercept(
            interceptor,
            "fake__rows",
            {
                "first": [{"blob": "x" * 200}],
                "second": [{"id": 2}],
            },
            mode="structured",
        )
        assert result.structured_content is not None
        assert result.structured_content["preview"] == ""
        assert result.structured_content["tables"][0]["source_path"] == "$.first"
        block = result.content[0]
        assert isinstance(block, types.TextContent)
        assert "preview (first 0 of 1 rows" in block.text


async def test_dual_channel_retention_accounts_for_both_channels() -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=10_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        value = {"items": [{"id": 1, "label": "dual"}]}
        await _intercept(interceptor, naming.mounted_name("fake", "rows"), value, mode="dual")
        row = store.connection.execute(
            f"SELECT result, result_text, result_blocks, result_structured FROM {ENVELOPE_TABLE}"
        ).fetchone()
        assert row is not None
        assert all(part is not None for part in row)
        assert store.retained_bytes > len(json.dumps(value).encode())


@pytest.mark.parametrize("mode", ["text", "structured", "dual"])
async def test_retention_charge_covers_each_stored_envelope_representation(mode: str) -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=100_000)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        value = {"items": [{"id": 1, "quote": 'a"b', "unicode": "☃"}]}
        await _intercept(interceptor, naming.mounted_name("fake", "rows"), value, mode=mode)
        row = store.connection.execute(
            f"SELECT args, result, result_text, result_blocks, result_structured, "
            f"flat_tables, source_paths FROM {ENVELOPE_TABLE}"
        ).fetchone()
        assert row is not None
        json_fields = (*row[0:2], row[3], row[4], row[5], row[6])
        stored_envelope_bytes = sum(
            len(str(field).encode("utf-8")) for field in json_fields if field is not None
        )
        text_bytes = len(row[2].encode("utf-8")) if row[2] is not None else 0
        assert store.retained_bytes >= stored_envelope_bytes + text_bytes


async def test_sequential_retention_evicts_oldest_call_coherently() -> None:
    limits = Limits(max_payload_bytes=500, max_session_bytes=1_000)
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
        with pytest.raises(QueryRejectedError, match="was evicted"):
            await QueryTool(store, limits).run(f'SELECT * FROM "{first_table}"')
        rows = store.connection.execute(
            f"SELECT tool, content_kinds, flat_tables, flat_reason, result IS NULL "
            f"FROM {ENVELOPE_TABLE} "
            "ORDER BY seq"
        ).fetchall()
        assert rows[0][0] == "rows"
        assert rows[0][1] == ["text"]
        assert rows[0][2] == []
        assert rows[0][3] == "retention_evicted"
        assert rows[0][4] is True
        assert rows[1][2] == [second_table]


async def test_out_of_order_planning_still_commits_in_arrival_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = Limits(max_payload_bytes=500, max_session_bytes=500, max_session_calls=10)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        original_build = interceptor._build_plans

        async def delayed_build(mounted, scope_id, selected):  # type: ignore[no-untyped-def]
            item_id = selected.value["items"][0]["id"]
            if item_id == 1:
                await anyio.sleep(0.02)
            return await original_build(mounted, scope_id, selected)

        monkeypatch.setattr(interceptor, "_build_plans", delayed_build)
        first, second = await asyncio.gather(
            _intercept(interceptor, "fake__rows", {"items": [{"id": 1}]}),
            _intercept(interceptor, "fake__rows", {"items": [{"id": 2}]}),
        )
        assert first.structured_content is not None
        assert second.structured_content is not None
        rows = store.connection.execute(
            f"SELECT seq, json_extract(result, '$.items[0].id'), flat_reason "
            f"FROM {ENVELOPE_TABLE} ORDER BY seq"
        ).fetchall()
        assert rows == [(1, None, "retention_evicted"), (2, "2", None)]
        assert first.structured_content["flat_reason"] is None
        assert second.structured_content["flat_reason"] is None
        assert first.structured_content["tables"][0]["name"] not in store.allowed_objects
        assert second.structured_content["tables"][0]["name"] in store.allowed_objects
        assert [
            first.structured_content["tables"][0]["row_count"],
            second.structured_content["tables"][0]["row_count"],
        ] == [1, 1]


async def test_preview_uses_bounded_raw_rows_not_storage_columns() -> None:
    limits = Limits(
        max_payload_bytes=20_000,
        max_session_bytes=100_000,
        preview_bytes=2_000,
        max_columns=2,
    )
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        value = {
            "items": [
                {"source-key": "kept", "another": "also", "third": "extra"},
                {"source-key": "second", "another": "row"},
            ],
            "tail": "x" * 2_500,
        }
        result = await _intercept(interceptor, naming.mounted_name("fake", "rows"), value)
        assert result.structured_content is not None
        preview = result.structured_content["preview"]
        assert '"source-key": "kept"' in preview
        assert "_extra" not in preview
        assert "__" not in preview


async def test_call_larger_than_retention_budget_degrades_safely() -> None:
    limits = Limits(max_payload_bytes=500, max_session_bytes=500)
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


async def test_over_budget_call_does_not_evict_healthy_retained_calls() -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=1_000, max_session_calls=10)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        mounted = naming.mounted_name("fake", "rows")
        first = await _intercept(interceptor, mounted, {"items": [{"id": 1}]})
        second = await _intercept(interceptor, mounted, {"items": [{"id": 2}]})
        assert first.structured_content is not None
        assert second.structured_content is not None
        first_table = first.structured_content["tables"][0]["name"]
        second_table = second.structured_content["tables"][0]["name"]
        third = await _intercept(interceptor, mounted, {"items": [{"id": 3, "padding": "z" * 800}]})
        assert third.structured_content is not None
        assert third.structured_content["tables"] == []
        assert third.structured_content["flat_reason"] == "retention_budget_exceeded"
        assert first_table in store.allowed_objects
        assert second_table in store.allowed_objects
        assert await QueryTool(store, limits).run(f'SELECT id FROM "{first_table}"')
        assert await QueryTool(store, limits).run(f'SELECT id FROM "{second_table}"')
        assert store.connection.execute(
            f"SELECT seq, flat_reason FROM {ENVELOPE_TABLE} ORDER BY seq"
        ).fetchall() == [(1, None), (2, None), (3, "retention_budget_exceeded")]


async def test_session_call_cap_deletes_old_unique_scopes() -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=100_000, max_session_calls=2)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        mounted = naming.mounted_name("fake", "rows")
        results = [
            await _intercept(interceptor, mounted, {"items": [{"id": index}]}) for index in range(3)
        ]
        assert all(result.structured_content is not None for result in results)
        first = results[0].structured_content
        assert first is not None
        first_scope = first["scope_id"]
        first_table = first["tables"][0]["name"]
        assert store.connection.execute(f"SELECT count(*) FROM {ENVELOPE_TABLE}").fetchone() == (2,)
        assert first_table not in store.allowed_objects
        assert f"{ENVELOPE_TABLE}__{first_scope}" not in store.allowed_objects
        assert len(store.retained_call_ids) <= 2


async def test_session_call_cap_keeps_shared_scope_view_until_last_row() -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=100_000, max_session_calls=2)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        mounted = naming.mounted_name("fake", "rows")
        first = await _intercept(
            interceptor, mounted, {"items": [{"id": 1}]}, meta={"sessionId": "same"}
        )
        second = await _intercept(
            interceptor, mounted, {"items": [{"id": 2}]}, meta={"sessionId": "same"}
        )
        third = await _intercept(
            interceptor, mounted, {"items": [{"id": 3}]}, meta={"sessionId": "same"}
        )
        assert first.structured_content is not None
        assert second.structured_content is not None
        assert third.structured_content is not None
        view = first.structured_content["envelope_table"]
        assert view in store.allowed_objects
        assert store.connection.execute(f"SELECT count(*) FROM {view}").fetchone() == (2,)


async def test_evicted_table_diagnostics_are_bounded() -> None:
    limits = Limits(max_payload_bytes=1_000, max_session_bytes=100_000, max_session_calls=1)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        mounted = naming.mounted_name("fake", "rows")
        for index in range(70):
            await _intercept(interceptor, mounted, {"items": [{"id": index}]})
        assert len(store.evicted_tables) <= 64
