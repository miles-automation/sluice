"""M2: results are recorded and replaced by a handle (spec 4, 5.1, 8)."""

import json
from datetime import UTC, datetime

import pytest
from mcp import types

from sluice import naming
from sluice.config import Limits
from sluice.intercept import Interceptor
from sluice.models import PayloadChannel
from sluice.proxy import Proxy
from sluice.store import ENVELOPE_TABLE, Store

pytestmark = pytest.mark.anyio


async def _run(
    interceptor: Interceptor, proxy: Proxy, tool: str, arguments: dict[str, object] | None = None
) -> types.CallToolResult:
    mounted = naming.mounted_name("fake", tool)
    result = await proxy.call(mounted, arguments)
    assert isinstance(result, types.CallToolResult)
    return await interceptor.intercept(
        server="fake",
        tool=tool,
        mounted=mounted,
        arguments=arguments,
        result=result,
        meta=None,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def _envelope(store: Store) -> list[tuple[object, ...]]:
    return store.connection.execute(
        f"SELECT tool, source_channel, channel_conflict, is_error, byte_size, wire_bytes, "
        f"result IS NULL FROM {ENVELOPE_TABLE}"
    ).fetchall()


async def test_payload_is_replaced_by_a_handle(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "rows", {"n": 400})
    text = _text(result)
    assert text.startswith("sluice: result recorded.")
    assert ENVELOPE_TABLE in text
    # The whole point: 400 rows did not come back in the content.
    assert len(text) < 4000
    assert len(_envelope(store)) == 1


async def test_structured_content_mirrors_the_handle(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    result = await _run(interceptor, proxy, "rows", {"n": 10})
    assert result.structured_content is not None
    assert result.structured_content["envelope_table"] == ENVELOPE_TABLE
    assert result.structured_content["source_channel"] == "text"
    assert result.structured_content["call_id"]
    assert result.structured_content["scope_id"]


async def test_structured_channel_wins_over_prose(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    """The blind spot from revision 1: flattening the prose summary would have
    discarded the data."""
    result = await _run(interceptor, proxy, "structured_only")
    assert result.structured_content is not None
    assert result.structured_content["source_channel"] == str(PayloadChannel.STRUCTURED)
    assert "Found 3 records" not in result.structured_content["preview"]
    assert '"items"' in result.structured_content["preview"]


async def test_channel_disagreement_is_surfaced(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "both_channels")
    assert result.structured_content is not None
    assert result.structured_content["channel_conflict"] is True
    assert "disagree" in _text(result)
    assert _envelope(store)[0][2] is True


async def test_non_json_text_is_recorded_without_a_table(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    result = await _run(interceptor, proxy, "just_text")
    assert result.structured_content is not None
    assert result.structured_content["source_channel"] == str(PayloadChannel.NONE)
    assert result.structured_content["flat_reason"].startswith("not_json")
    assert "no json here" in result.structured_content["preview"]


async def test_small_payloads_are_previewed_in_full(interceptor: Interceptor, proxy: Proxy) -> None:
    """FR-14. Always-intercept costs a round trip only if the preview withholds
    something, so a payload under the budget is reproduced complete."""
    result = await _run(interceptor, proxy, "rows", {"n": 2})
    assert result.structured_content is not None
    assert result.structured_content["preview_complete"] is True
    assert "preview (complete," in _text(result)
    assert json.loads(result.structured_content["preview"])["items"][1]["id"] == 1


async def test_large_payloads_are_truncated_and_say_so(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    result = await _run(interceptor, proxy, "rows", {"n": 400})
    assert result.structured_content is not None
    assert result.structured_content["preview_complete"] is False
    assert "preview (first 3 of 400 rows," in _text(result)


async def test_errors_pass_through_and_are_still_recorded(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "boom")
    assert result.is_error is True
    assert _text(result) == "downstream says no"
    assert result.structured_content is None
    rows = _envelope(store)
    assert rows[0][3] is True


async def test_image_results_pass_through_and_are_still_recorded(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "picture")
    assert isinstance(result.content[0], types.ImageContent)
    assert len(_envelope(store)) == 1


async def test_image_bytes_are_not_stored(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    """FR-13: the envelope records block types and sizes, not the bytes."""
    await _run(interceptor, proxy, "picture")
    blocks = store.connection.execute(f"SELECT result_blocks FROM {ENVELOPE_TABLE}").fetchall()
    assert "iVBOR" not in blocks[0][0]
    assert '"image"' in blocks[0][0]


async def test_oversize_payloads_pass_through_unparsed(proxy: Proxy, store: Store) -> None:
    tight = Limits(max_payload_bytes=64)
    interceptor = Interceptor(store, tight)
    result = await _run(interceptor, proxy, "rows", {"n": 400})
    assert "over the 64-byte materialization ceiling" in _text(
        result.model_copy(update={"content": result.content[-1:]})
    )
    # Payload columns stay NULL: no parse happened.
    row = _envelope(store)[0]
    assert row[6] is True
    wire = row[5]
    assert isinstance(wire, int)
    assert wire > 64


async def test_no_query_hint_until_the_query_tool_exists(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    """The handle must not point at a tool that is not mounted yet (plan M4)."""
    result = await _run(interceptor, proxy, "rows", {"n": 2})
    assert "`query` tool" not in _text(result)


async def test_envelope_write_failure_does_not_fail_the_call(proxy: Proxy, store: Store) -> None:
    """FR-8 is conditional on the write. Losing the audit row must not lose the
    tool result the agent asked for."""
    store.close()
    interceptor = Interceptor(store, Limits())
    result = await _run(interceptor, proxy, "rows", {"n": 2})
    assert '"items"' in _text(result)
    assert result.structured_content is None


async def test_full_loop_returns_a_handle_not_a_payload(fake_config: object) -> None:
    """Two hops with interception on: client -> sluice -> fake downstream."""
    from contextlib import AsyncExitStack

    from mcp import Client

    from sluice.config import Config
    from sluice.server import build_server

    assert isinstance(fake_config, Config)
    async with AsyncExitStack() as stack:
        started = await Proxy.start(fake_config, stack)
        with Store.open(fake_config.limits) as opened:
            server = build_server(started, Interceptor(opened, fake_config.limits))
            async with Client(server) as client:
                mounted = naming.mounted_name("fake", "rows")
                result = await client.call_tool(mounted, {"n": 400})
    text = _text(result)
    assert "sluice: result recorded." in text
    assert "row-0399" not in text
    assert result.structured_content is not None
    assert result.structured_content["call_id"]


# --------------------------------------------------------------------------
# M3: materialization
# --------------------------------------------------------------------------


def _columns(result: types.CallToolResult, table: int = 0) -> dict[str, str]:
    assert result.structured_content is not None
    spec = result.structured_content["tables"][table]
    return {column["name"]: column["type"] for column in spec["columns"]}


async def test_rows_are_materialized_with_real_types(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "rows", {"n": 400})
    assert result.structured_content is not None
    table = result.structured_content["tables"][0]
    assert table["row_count"] == 400
    assert table["source_path"] == "$.items"
    assert _columns(result) == {
        "id": "BIGINT",
        "name": "VARCHAR",
        "score": "DOUBLE",
        "tag": "VARCHAR",
        "active": "BOOLEAN",
    }
    rows = store.connection.execute(f'SELECT count(*) FROM "{table["name"]}"').fetchall()
    assert rows[0][0] == 400


async def test_aggregates_match_the_source_data(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    """A small version of the M5 correctness property: the whole point of the
    project, checked end to end for the first time."""
    import statistics

    from tests.fake_server import rows_payload

    result = await _run(interceptor, proxy, "rows", {"n": 400})
    assert result.structured_content is not None
    name = result.structured_content["tables"][0]["name"]
    source = rows_payload(400)
    scores = [row["score"] for row in source]

    count, minimum, maximum, median = store.connection.execute(
        f'SELECT count(score), min(score), max(score), median(score) FROM "{name}"'
    ).fetchall()[0]
    assert count == len(scores)
    assert minimum == min(scores)
    assert maximum == max(scores)
    assert median == statistics.median(scores)

    grouped = store.connection.execute(
        f'SELECT tag, count(*) FROM "{name}" GROUP BY tag ORDER BY tag'
    ).fetchall()
    from collections import Counter

    expected = sorted(Counter(row["tag"] for row in source).items())
    assert grouped == expected


async def test_every_candidate_array_becomes_its_own_table(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    result = await _run(interceptor, proxy, "two_arrays")
    assert result.structured_content is not None
    tables = result.structured_content["tables"]
    assert [t["source_path"] for t in tables] == ["$.rows", "$.facets"]
    assert [t["row_count"] for t in tables] == [20, 100]
    assert "also materialized" not in _text(result) or len(tables) == 2
    assert tables[0]["name"] != tables[1]["name"]


async def test_mixed_elements_produce_no_table(interceptor: Interceptor, proxy: Proxy) -> None:
    result = await _run(interceptor, proxy, "mixed_elements")
    assert result.structured_content is not None
    assert result.structured_content["tables"] == []
    assert "mixed_elements" in result.structured_content["flat_reason"]


async def test_empty_result_says_so(interceptor: Interceptor, proxy: Proxy) -> None:
    result = await _run(interceptor, proxy, "empty")
    assert result.structured_content is not None
    assert result.structured_content["tables"] == []
    assert result.structured_content["flat_reason"].startswith("empty")
    assert "no table:" in _text(result)


async def test_nested_values_become_json_columns(interceptor: Interceptor, proxy: Proxy) -> None:
    result = await _run(interceptor, proxy, "nested")
    columns = _columns(result)
    assert columns["meta"] == "JSON"
    assert columns["tags"] == "JSON"
    assert columns["id"] == "BIGINT"
    assert "json_extract" in _text(result)


async def test_json_columns_are_queryable(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    result = await _run(interceptor, proxy, "nested")
    assert result.structured_content is not None
    name = result.structured_content["tables"][0]["name"]
    total = store.connection.execute(
        f"SELECT sum(CAST(json_extract(meta, '$.k') AS BIGINT)) FROM \"{name}\""
    ).fetchall()[0][0]
    assert total == sum(range(5))


async def test_wide_payloads_are_capped_with_an_extra_column(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    result = await _run(interceptor, proxy, "wide", {"k": 200})
    columns = _columns(result)
    assert len(columns) == 65
    assert columns["_extra"] == "JSON"


async def test_edge_numbers_are_flagged_inexact(interceptor: Interceptor, proxy: Proxy) -> None:
    """The correctness guarantee lapses outside spec 5.6's domain, and the
    handle has to say so per column."""
    result = await _run(interceptor, proxy, "edge_numbers")
    assert result.structured_content is not None
    columns = {c["name"]: c for c in result.structured_content["tables"][0]["columns"]}
    assert columns["big"]["type"] == "BIGINT"
    assert columns["big"]["exact"] is True
    assert columns["huge"]["type"] == "HUGEINT"
    assert columns["mid"]["type"] == "BIGINT"
    assert columns["f"]["type"] == "DOUBLE"
    assert columns["f"]["exact"] is False  # non-finite present
    assert "inexact" in _text(result)


async def test_scalar_arrays_get_a_value_column(interceptor: Interceptor, proxy: Proxy) -> None:
    result = await _run(interceptor, proxy, "scalars", {"n": 10})
    assert _columns(result) == {"value": "DOUBLE"}


async def test_colliding_tool_names_get_separate_tables(
    interceptor: Interceptor, proxy: Proxy
) -> None:
    first = await _run(interceptor, proxy, "hyphen-tool")
    second = await _run(interceptor, proxy, "hyphen_tool")
    assert first.structured_content is not None
    assert second.structured_content is not None
    assert (
        first.structured_content["tables"][0]["name"]
        != second.structured_content["tables"][0]["name"]
    )


async def test_flat_table_rows_are_traceable_to_the_call(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    """`_row` and `_call_id` are the join back to the envelope, and the handle's
    call_id is what the agent uses to find it. All three have to agree."""
    result = await _run(interceptor, proxy, "rows", {"n": 5})
    assert result.structured_content is not None
    call_id = result.structured_content["call_id"]
    name = result.structured_content["tables"][0]["name"]

    rows = store.connection.execute(
        f'SELECT _row, _call_id, id FROM "{name}" ORDER BY _row'
    ).fetchall()
    assert [r[0] for r in rows] == [0, 1, 2, 3, 4]
    assert {r[1] for r in rows} == {call_id}
    assert [r[2] for r in rows] == [0, 1, 2, 3, 4]

    envelope = store.connection.execute(
        f"SELECT call_id, flat_tables, source_paths FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert envelope == (call_id, [name], ["$.items"])


async def test_downstream_structured_content_is_preserved_on_the_envelope(
    interceptor: Interceptor, proxy: Proxy, store: Store
) -> None:
    """Sluice overwrites the outgoing structuredContent with its handle, so the
    envelope is the only place the downstream tool's own structured output
    survives."""
    await _run(interceptor, proxy, "structured_only")
    stored, text = store.connection.execute(
        f"SELECT result_structured, result_text FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert json.loads(stored) == {"items": [{"id": i, "score": i * 2} for i in range(3)]}
    assert text == "Found 3 records for your query."


async def test_no_orphan_tables_when_materialization_fails(
    proxy: Proxy, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tables and the envelope go in as one transaction, so a failure leaves the
    database untouched rather than leaving tables nothing points at."""
    from sluice import intercept as intercept_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("insert blew up")

    interceptor = Interceptor(store, Limits())
    monkeypatch.setattr(store, "_create_flat", explode)
    result = await _run(interceptor, proxy, "rows", {"n": 5})

    tables = store.connection.execute(
        "SELECT table_name FROM duckdb_tables() WHERE table_name != ?", [ENVELOPE_TABLE]
    ).fetchall()
    assert tables == []
    # The call is still recorded, and the handle says why there is no table.
    row = store.connection.execute(
        f"SELECT flat_tables, flat_reason FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert row[0] == []
    assert row[1].startswith("load_failed")
    assert result.structured_content is not None
    assert result.structured_content["tables"] == []
    assert intercept_module is not None
