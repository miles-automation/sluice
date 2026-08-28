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
    assert result.structured_content["flat_reason"] == "not_json"
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
    assert "preview (truncated," in _text(result)


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
