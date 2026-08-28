"""M1: results come back semantically unchanged.

Comparison is on parsed SDK models, not raw bytes. The SDK deserializes and
reserializes on both hops, so key order, unicode escaping, and whitespace may
differ while the result is unchanged. Asserting byte-identity would fail on
differences that do not matter and would tempt someone to weaken the test.
"""

import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters, types

from sluice import naming
from sluice.config import Config
from sluice.proxy import Proxy
from sluice.server import build_server

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]


def _downstream_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "tests.fake_server"], cwd=str(REPO_ROOT)
    )


async def _direct(tool: str, arguments: dict[str, object] | None = None) -> types.CallToolResult:
    async with Client(_downstream_params()) as client:
        result = await client.session.call_tool(tool, arguments, allow_input_required=True)
    assert isinstance(result, types.CallToolResult)
    return result


@pytest.mark.parametrize(
    "tool",
    ["rows", "boom", "picture", "just_text", "structured_only", "both_channels", "rich_result"],
)
async def test_result_matches_a_direct_call(proxy: Proxy, tool: str) -> None:
    """Compares the WHOLE parsed model, not a chosen subset.

    Checking only content, structured_content, and is_error let a mutation that
    discarded every downstream result `_meta` pass the entire suite. Nothing is
    excluded here because nothing needs to be: an extra hop through Sluice
    leaves the serialized result byte-for-byte equal.
    """
    through = await proxy.call(naming.mounted_name("fake", tool), {"n": 5})
    direct = await _direct(tool, {"n": 5})
    assert isinstance(through, types.CallToolResult)
    assert through.model_dump(mode="json") == direct.model_dump(mode="json")


async def test_result_metadata_and_annotations_survive_the_hop(proxy: Proxy) -> None:
    """Named separately from the parametrized case so a regression says which
    thing was lost."""
    result = await proxy.call(naming.mounted_name("fake", "rich_result"), None)
    assert isinstance(result, types.CallToolResult)
    # Subset, not equality: the SDK adds `io.modelcontextprotocol/serverInfo` to
    # result `_meta` on the way out. What matters is that the downstream tool's
    # own keys are still there.
    assert result.meta is not None
    assert result.meta["vendor.example/requestId"] == "req-42"
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert block.meta == {"vendor.example/trace": "abc123"}
    assert block.annotations is not None
    assert block.annotations.audience == ["user"]
    assert block.annotations.priority == 0.7


async def test_errors_are_forwarded_verbatim(proxy: Proxy) -> None:
    """`tool_error` is the one failure class that survives as-is (spec 8)."""
    result = await proxy.call(naming.mounted_name("fake", "boom"), None)
    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert block.text == "downstream says no"


async def test_image_content_is_not_materialized(proxy: Proxy) -> None:
    result = await proxy.call(naming.mounted_name("fake", "picture"), None)
    assert isinstance(result, types.CallToolResult)
    assert isinstance(result.content[0], types.ImageContent)


async def test_full_loop_through_the_sluice_server(fake_config: Config) -> None:
    """Two hops: client -> sluice -> fake downstream."""
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        proxy = await Proxy.start(fake_config, stack)
        async with Client(build_server(proxy)) as client:
            listing = await client.list_tools()
            names = {tool.name for tool in listing.tools}
            assert naming.mounted_name("fake", "rows") in names

            wanted = naming.mounted_name("fake", "rows")
            mounted = next(t for t in listing.tools if t.name == wanted)
            assert mounted.output_schema is None

            result = await client.call_tool(mounted.name, {"n": 3})
            block = result.content[0]
            assert isinstance(block, types.TextContent)
            assert '"items"' in block.text
