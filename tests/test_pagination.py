"""Bounded pagination through the whole product path (spec 002)."""

import json
import statistics
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from mcp import Client, types

from sluice import naming
from sluice.config import Config, ConfigError, Limits, PaginationConfig, parse_config
from sluice.errors import DownstreamError, FailureClass
from sluice.intercept import Interceptor
from sluice.paginate import PaginationArgumentError, StopReason, fetch_all
from sluice.proxy import Proxy
from sluice.query import QueryTool
from sluice.server import build_server
from sluice.store import Store
from tests.fake_server import rows_payload

pytestmark = pytest.mark.anyio

PAGED = naming.collection_name("fake", "paged")


def _config(base: Config, **pagination: PaginationConfig) -> Config:
    return Config(server=base.server, limits=base.limits, pagination=dict(pagination))


@asynccontextmanager
async def _client(config: Config, store: Store) -> AsyncIterator[Client]:
    async with AsyncExitStack() as stack:
        proxy = await Proxy.start(config, stack)
        server = build_server(
            proxy,
            Interceptor(store, config.limits, query_available=True),
            QueryTool(store, config.limits),
        )
        async with Client(server) as client:
            yield client


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return dict(result.structured_content)


def _pagination(result: types.CallToolResult) -> dict[str, Any]:
    pagination = _structured(result)["pagination"]
    assert isinstance(pagination, dict)
    return pagination


def _table(result: types.CallToolResult) -> str:
    tables = _structured(result)["tables"]
    assert isinstance(tables, list) and len(tables) == 1
    return str(tables[0]["name"])


# --- configuration -----------------------------------------------------------


def test_pagination_config_defaults() -> None:
    config = parse_config({"servers": {"s": {"command": "x"}}, "pagination": {"list": {}}})
    assert config.pagination["list"] == PaginationConfig(tool="list")
    assert config.pagination["list"].page_size == 200


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"nope": 1}, "unknown keys"),
        ({"page_size": 0}, "at least 1"),
        ({"page_size": "10"}, "must be an integer"),
        ({"max_pages": True}, "must be an integer"),
        ({"max_seconds": 0}, "positive finite"),
        ({"max_seconds": "fast"}, "must be a number"),
        ({"items": ""}, "non-empty string"),
        ({"limit_arg": "x", "offset_arg": "x"}, "must differ"),
        ({"max_bytes": 10**12}, "max_session_bytes"),
    ],
)
def test_pagination_config_rejects(entry: dict[str, Any], message: str) -> None:
    raw = {"servers": {"s": {"command": "x"}}, "pagination": {"list": entry}}
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_pagination_must_be_a_table() -> None:
    with pytest.raises(ConfigError, match="table"):
        parse_config({"servers": {"s": {"command": "x"}}, "pagination": {"list": 3}})


# --- startup refusals --------------------------------------------------------


async def test_startup_refuses_unknown_tool(fake_config: Config) -> None:
    config = _config(fake_config, absent=PaginationConfig(tool="absent"))
    async with AsyncExitStack() as stack:
        with pytest.raises(ConfigError, match="did not list"):
            await Proxy.start(config, stack)


async def test_startup_refuses_tool_without_read_only_annotation(fake_config: Config) -> None:
    config = _config(fake_config, paged_mutable=PaginationConfig(tool="paged_mutable"))
    async with AsyncExitStack() as stack:
        with pytest.raises(ConfigError, match="readOnlyHint"):
            await Proxy.start(config, stack)


# --- listing -----------------------------------------------------------------


async def test_collection_is_listed_next_to_the_original(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged"))
    async with _client(config, store) as client:
        listing = await client.list_tools()
    names = {tool.name for tool in listing.tools}
    assert naming.mounted_name("fake", "paged") in names
    assert PAGED in names
    collection = next(tool for tool in listing.tools if tool.name == PAGED)
    assert "limit" not in collection.input_schema["properties"]
    assert "offset" in collection.input_schema["properties"]
    assert collection.input_schema["required"] == []
    assert collection.annotations is not None
    assert collection.annotations.read_only_hint is True
    assert collection.output_schema is None
    assert "every page of `paged`" in (collection.description or "")
    single = next(
        tool for tool in listing.tools if tool.name == naming.mounted_name("fake", "paged")
    )
    assert f"call `{PAGED}` instead" in (single.description or "")
    untouched = next(
        tool for tool in listing.tools if tool.name == naming.mounted_name("fake", "rows")
    )
    assert "instead" not in (untouched.description or "")


# --- complete fetches --------------------------------------------------------


async def test_complete_fetch_lands_every_row_in_one_table(
    fake_config: Config, store: Store
) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 450})
        assert result.is_error is not True
        text = _text(result)
        assert "pagination: complete  pages=5  rows=450" in text
        pagination = _pagination(result)
        assert pagination["status"] == "complete"
        assert pagination["resume_offset"] is None
        table = _table(result)
        assert "__all__" in table
        assert _structured(result)["tables"][0]["row_count"] == 450

        count = await client.call_tool("query", {"sql": f'SELECT count(*) FROM "{table}"'})
        assert "| 450 |" in _text(count)
        median = await client.call_tool(
            "query", {"sql": f'SELECT median(score) AS m FROM "{table}"'}
        )
        expected = statistics.median(row["score"] for row in rows_payload(450))
        assert f"| {expected} |" in _text(median)
        grouped = await client.call_tool(
            "query",
            {"sql": f'SELECT tag, count(*) AS n FROM "{table}" GROUP BY tag ORDER BY tag'},
        )
        counts = Counter(row["tag"] for row in rows_payload(450))
        for tag, n in sorted(counts.items()):
            assert f"| {tag} | {n} |" in _text(grouped)


async def test_a_single_page_is_still_a_collection(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 7})
    assert _pagination(result)["pages"] == 1
    assert _structured(result)["tables"][0]["row_count"] == 7


async def test_resume_from_an_offset(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 450, "offset": 300})
        table = _table(result)
        ids = await client.call_tool(
            "query", {"sql": f'SELECT min(id) AS lo, max(id) AS hi, count(*) AS n FROM "{table}"'}
        )
    assert _pagination(result)["pages"] == 2
    assert "| 300 | 449 | 150 |" in _text(ids)


async def test_envelope_records_the_collection_call(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 250})
        envelope = str(_structured(result)["envelope_table"])
        call_id = _structured(result)["call_id"]
        sql = f"SELECT tool, args FROM {naming.quote_ident(envelope)} WHERE call_id = '{call_id}'"
        row = await client.call_tool("query", {"sql": sql})
    text = _text(row)
    assert "| paged |" in text
    assert "sluice_pagination" in text


# --- limits and partial results --------------------------------------------


async def test_max_pages_yields_a_partial_result_with_a_resume_offset(
    fake_config: Config, store: Store
) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100, max_pages=2))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 450})
        table = _table(result)
        count = await client.call_tool("query", {"sql": f'SELECT count(*) FROM "{table}"'})
    text = _text(result)
    assert "pagination: PARTIAL, stopped by max_pages" in text
    assert "call again with offset=200" in text
    pagination = _pagination(result)
    assert pagination == {
        **pagination,
        "status": "partial",
        "reason": "max_pages",
        "pages": 2,
        "rows": 200,
        "resume_offset": 200,
    }
    assert "| 200 |" in _text(count)


async def test_max_bytes_stops_after_the_page_that_crossed_it(
    fake_config: Config, store: Store
) -> None:
    config = _config(
        fake_config, paged=PaginationConfig(tool="paged", page_size=100, max_bytes=10_000)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 450})
    pagination = _pagination(result)
    assert pagination["status"] == "partial"
    assert pagination["reason"] == "max_bytes"
    assert pagination["bytes"] >= 10_000
    assert pagination["pages"] < 5
    assert pagination["resume_offset"] == pagination["rows"]


@pytest.mark.slow
async def test_max_seconds_stops_the_fetch_and_leaves_the_proxy_usable(
    fake_config: Config, store: Store
) -> None:
    config = _config(
        fake_config,
        paged_slow=PaginationConfig(tool="paged_slow", page_size=100, max_seconds=0.5),
    )
    slow = naming.collection_name("fake", "paged_slow")
    async with _client(config, store) as client:
        result = await client.call_tool(slow, {"n": 450, "delay_ms": 300})
        pagination = _pagination(result)
        after = await client.call_tool(naming.mounted_name("fake", "rows"), {"n": 3})
    assert pagination["status"] == "partial"
    assert pagination["reason"] == "max_seconds"
    assert 1 <= pagination["pages"] < 5
    assert pagination["resume_offset"] == pagination["rows"]
    assert after.is_error is not True


async def test_non_advancing_offset_is_a_partial_result(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged_stuck=PaginationConfig(tool="paged_stuck", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(naming.collection_name("fake", "paged_stuck"), {"n": 450})
    pagination = _pagination(result)
    assert pagination["reason"] == "offset_not_advancing"
    assert pagination["pages"] == 1
    assert pagination["rows"] == 100
    assert pagination["resume_offset"] is None
    assert "cannot be resumed safely" in _text(result)


async def test_missing_next_offset_is_a_partial_result(fake_config: Config, store: Store) -> None:
    config = _config(
        fake_config, paged_no_next=PaginationConfig(tool="paged_no_next", page_size=100)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(naming.collection_name("fake", "paged_no_next"), {"n": 450})
    pagination = _pagination(result)
    assert pagination["reason"] == "missing_next_offset"
    assert pagination["pages"] == 1
    assert pagination["resume_offset"] is None


async def test_a_failed_later_page_keeps_the_pages_before_it(
    fake_config: Config, store: Store
) -> None:
    config = _config(
        fake_config, paged_error_at=PaginationConfig(tool="paged_error_at", page_size=100)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(
            naming.collection_name("fake", "paged_error_at"), {"n": 450}
        )
        table = _table(result)
        count = await client.call_tool("query", {"sql": f'SELECT count(*) FROM "{table}"'})
    assert result.is_error is not True
    pagination = _pagination(result)
    assert pagination["reason"] == "page_error"
    assert pagination["detail"] == "page unavailable"
    assert pagination["pages"] == 2
    assert pagination["resume_offset"] == 200
    assert "| 200 |" in _text(count)


async def test_a_failed_first_page_is_an_error_not_an_empty_table(
    fake_config: Config, store: Store
) -> None:
    config = _config(
        fake_config, paged_error_at=PaginationConfig(tool="paged_error_at", page_size=100)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(
            naming.collection_name("fake", "paged_error_at"), {"n": 450, "offset": 200}
        )
    assert result.is_error is True
    texts = [block.text for block in result.content if isinstance(block, types.TextContent)]
    assert texts[0] == "page unavailable"
    assert "fetched no pages (page_error)" in texts[-1]
    assert result.structured_content is None


async def test_malformed_page_is_a_partial_result(fake_config: Config, store: Store) -> None:
    config = _config(
        fake_config, paged_bad_items=PaginationConfig(tool="paged_bad_items", page_size=100)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(
            naming.collection_name("fake", "paged_bad_items"), {"n": 450}
        )
    assert result.is_error is True
    assert "malformed_page" in _text(result)
    assert "not an array" in _text(result)


async def test_interactive_page_stops_the_fetch(fake_config: Config, store: Store) -> None:
    config = _config(
        fake_config, paged_interactive=PaginationConfig(tool="paged_interactive", page_size=100)
    )
    async with _client(config, store) as client:
        result = await client.call_tool(
            naming.collection_name("fake", "paged_interactive"), {"n": 450}
        )
    pagination = _pagination(result)
    assert pagination["reason"] == "interactive"
    assert pagination["pages"] == 1
    assert pagination["resume_offset"] == 100


async def test_agent_supplied_limit_is_rejected(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(PAGED, {"n": 450, "limit": 5})
    assert result.is_error is True
    assert "managed by Sluice" in _text(result)


async def test_original_tool_still_returns_one_page(fake_config: Config, store: Store) -> None:
    config = _config(fake_config, paged=PaginationConfig(tool="paged", page_size=100))
    async with _client(config, store) as client:
        result = await client.call_tool(
            naming.mounted_name("fake", "paged"), {"n": 450, "limit": 30, "offset": 0}
        )
    assert _structured(result)["pagination"] is None
    assert _structured(result)["tables"][0]["row_count"] == 30


# --- the loop on its own -----------------------------------------------------


def _page_result(items: list[Any], has_more: bool, next_offset: object) -> types.CallToolResult:
    payload = {"items": items, "has_more": has_more, "next_offset": next_offset}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))])


async def test_loop_forwards_filters_and_managed_arguments() -> None:
    seen: list[dict[str, Any]] = []

    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        seen.append(arguments)
        offset = int(arguments["offset"])
        return _page_result([{"i": offset}], has_more=offset < 20, next_offset=offset + 10)

    outcome = await fetch_all(call, PaginationConfig(tool="t", page_size=10), {"q": "x"})
    assert outcome.reason is StopReason.COMPLETE
    assert seen == [
        {"q": "x", "limit": 10, "offset": 0},
        {"q": "x", "limit": 10, "offset": 10},
        {"q": "x", "limit": 10, "offset": 20},
    ]
    assert outcome.rows == [{"i": 0}, {"i": 10}, {"i": 20}]


async def test_loop_reports_a_transport_failure_as_partial() -> None:
    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        if arguments["offset"] > 0:
            raise DownstreamError(FailureClass.TRANSPORT, "t", "pipe closed")
        return _page_result([{"i": 0}], has_more=True, next_offset=10)

    outcome = await fetch_all(call, PaginationConfig(tool="t", page_size=10), None)
    assert outcome.reason is StopReason.PAGE_FAILED
    assert outcome.detail is not None and "pipe closed" in outcome.detail
    assert outcome.pages == 1
    assert outcome.resume_offset == 10


async def test_loop_treats_a_repeated_offset_as_not_advancing() -> None:
    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        offset = int(arguments["offset"])
        return _page_result([{"i": offset}], has_more=True, next_offset=10 if offset == 0 else 0)

    outcome = await fetch_all(call, PaginationConfig(tool="t", page_size=10), None)
    assert outcome.reason is StopReason.NOT_ADVANCING
    assert outcome.pages == 2


async def test_loop_treats_an_empty_page_with_has_more_as_not_advancing() -> None:
    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        return _page_result([], has_more=True, next_offset=int(arguments["offset"]) + 10)

    outcome = await fetch_all(call, PaginationConfig(tool="t", page_size=10), None)
    assert outcome.reason is StopReason.NOT_ADVANCING
    assert outcome.pages == 1


async def test_loop_rejects_bad_resume_offsets() -> None:
    async def call(arguments: dict[str, Any]) -> types.CallToolResult:
        return _page_result([], has_more=False, next_offset=None)

    for bad in (-1, True, "0"):
        with pytest.raises(PaginationArgumentError):
            await fetch_all(call, PaginationConfig(tool="t"), {"offset": bad})


def test_collection_names_are_distinct_from_mounted_names() -> None:
    assert naming.collection_name("s", "t") != naming.mounted_name("s", "t")
    assert naming.collection_name("s", "t") != naming.collection_name("s", "t.all")
    assert Limits().max_payload_bytes < PaginationConfig(tool="t").max_bytes
