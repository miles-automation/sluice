"""M1: the proxy layer. No database yet."""

import json

import anyio
import pytest
from mcp import Client, types

from sluice import naming
from sluice.config import Config
from sluice.errors import DownstreamError, FailureClass
from sluice.proxy import HANDLE_NOTE, Proxy
from tests.fake_server import PAGE_TWO_TOOL, build_fake_server

pytestmark = pytest.mark.anyio


def _mounted(tool: str) -> str:
    return naming.mounted_name("fake", tool)


async def test_page_two_tool_is_mounted(proxy: Proxy) -> None:
    """The defect this pins: list_tools() returns one page and does not follow
    next_cursor, so a tool only on page 2 would never be mounted while every
    single-page test stayed green."""
    names = {tool.name for tool in proxy.mounted_tools()}
    assert _mounted(PAGE_TWO_TOOL) in names


@pytest.mark.contract
async def test_sdk_list_tools_really_does_return_one_page() -> None:
    """Guards the reason the pagination loop exists. If the SDK starts
    paginating for us, this fails and the loop can be reconsidered."""
    async with Client(build_fake_server()) as client:
        page = await client.list_tools()
    assert page.next_cursor is not None
    assert PAGE_TWO_TOOL not in {tool.name for tool in page.tools}


async def test_colliding_tool_names_are_both_mounted(proxy: Proxy) -> None:
    names = {tool.name for tool in proxy.mounted_tools()}
    assert _mounted("hyphen-tool") in names
    assert _mounted("hyphen_tool") in names
    assert _mounted("hyphen-tool") != _mounted("hyphen_tool")


async def test_annotations_survive_cloning(proxy: Proxy) -> None:
    """Rebuilding the tool field by field would drop destructive_hint, turning
    off the signal a client uses to ask the user for confirmation."""
    tool = next(t for t in proxy.mounted_tools() if t.name == _mounted("destructive"))
    assert tool.annotations is not None
    assert tool.annotations.destructive_hint is True
    assert tool.title == "Destructive Thing"


async def test_output_schema_is_removed(proxy: Proxy) -> None:
    entry = proxy.resolve(_mounted("bad_schema"))
    assert entry is not None
    assert entry.original.output_schema is not None
    assert entry.exposed.output_schema is None


async def test_description_keeps_upstream_text_and_appends_the_note(proxy: Proxy) -> None:
    tool = next(t for t in proxy.mounted_tools() if t.name == _mounted("rows"))
    assert tool.description is not None
    assert tool.description.startswith("n homogeneous objects")
    assert tool.description.endswith(HANDLE_NOTE)


async def test_input_schema_is_passed_through(proxy: Proxy) -> None:
    entry = proxy.resolve(_mounted("rows"))
    assert entry is not None
    assert entry.exposed.input_schema == entry.original.input_schema


async def test_call_forwards_and_returns_the_payload(proxy: Proxy) -> None:
    result = await proxy.call(_mounted("rows"), {"n": 5})
    assert isinstance(result, types.CallToolResult)
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert len(json.loads(block.text)["items"]) == 5


async def test_unknown_tool_is_a_protocol_failure(proxy: Proxy) -> None:
    with pytest.raises(DownstreamError) as caught:
        await proxy.call("no_such_tool", None)
    assert caught.value.failure_class is FailureClass.PROTOCOL


async def test_output_schema_violation_is_classified(proxy: Proxy) -> None:
    """The SDK raises rather than returning a result, so there is nothing to
    forward verbatim. It must not escape as a bare RuntimeError."""
    with pytest.raises(DownstreamError) as caught:
        await proxy.call(_mounted("bad_schema"), None)
    assert caught.value.failure_class is FailureClass.OUTPUT_SCHEMA


async def test_transport_failure_marks_the_session_unhealthy(
    proxy: Proxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def broken_call(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise anyio.EndOfStream

    monkeypatch.setattr(proxy.session, "call_tool", broken_call)
    with pytest.raises(DownstreamError) as first:
        await proxy.call(_mounted("rows"), None)
    assert first.value.failure_class is FailureClass.TRANSPORT
    assert proxy.healthy is False

    with pytest.raises(DownstreamError, match="session is unhealthy") as second:
        await proxy.call(_mounted("rows"), None)
    assert second.value.failure_class is FailureClass.TRANSPORT
    assert calls == 1


async def test_round_trips_are_relayed_not_answered(proxy: Proxy) -> None:
    """Sluice must hand the elicitation back upstream. Answering it itself would
    mean replying to a prompt addressed to the real client and its human."""
    first = await proxy.call(_mounted("needs_input"), None)
    assert isinstance(first, types.InputRequiredResult)
    assert first.request_state == "awaiting-pick"
    assert first.input_requests is not None
    assert "pick" in first.input_requests

    second = await proxy.call(
        _mounted("needs_input"),
        None,
        request_state=first.request_state,
        input_responses={"pick": types.ElicitResult(action="accept", content={"n": 7})},
    )
    assert isinstance(second, types.CallToolResult)
    block = second.content[0]
    assert isinstance(block, types.TextContent)
    assert json.loads(block.text)["items"][0]["round"] == 2


async def test_sluice_advertises_no_sampling_or_elicitation(proxy: Proxy) -> None:
    """Advertising either would invite the downstream server to ask Sluice."""
    result = await proxy.call(_mounted("client_capabilities"), None)
    assert isinstance(result, types.CallToolResult)
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert json.loads(block.text) == {"sampling": False, "elicitation": False}


async def test_startup_fails_loudly_on_a_mounted_name_collision(
    fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two downstream tools colliding used to overwrite each other in a dict,
    leaving the agent able to call one tool and reach another. Forced here,
    because a wider digest makes a natural collision unlikely, not impossible."""
    from contextlib import AsyncExitStack

    from sluice.__main__ import first_startup_error

    monkeypatch.setattr(naming, "tag", lambda value: "constant")
    with pytest.raises(BaseException) as caught:  # anyio wraps it in a group
        async with AsyncExitStack() as stack:
            await Proxy.start(fake_config, stack)
    found = first_startup_error(caught.value)
    assert isinstance(found, naming.NameCollisionError)
    assert "both mount as" in str(found)
