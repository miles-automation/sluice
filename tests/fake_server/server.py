"""The fake downstream server (plan M1)."""

import json
import math
from typing import Any

import anyio
from mcp import types
from mcp.server import Server, ServerRequestContext

PAGE_TWO_TOOL = "page_two_only"
"""Mounted only if the client follows `next_cursor`. See plan M1."""

PAGE_TWO_CURSOR = "p2"

_TAGS = ("alpha", "beta", "gamma")


def rows_payload(n: int) -> list[dict[str, Any]]:
    """Deterministic homogeneous rows. `score` is the column tests aggregate."""
    return [
        {
            "id": i,
            "name": f"row-{i:04d}",
            "score": round((i * 37 % 1000) / 7.0, 4),
            "tag": _TAGS[i % len(_TAGS)],
            "active": i % 2 == 0,
        }
        for i in range(n)
    ]


def _text(payload: object) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(payload))]


def _ok(payload: object) -> types.CallToolResult:
    return types.CallToolResult(content=_text(payload))


def _int_arg(arguments: dict[str, Any] | None, name: str, default: int) -> int:
    if not arguments:
        return default
    value = arguments.get(name, default)
    return int(value) if isinstance(value, int | float | str) else default


def _tool(name: str, description: str, **extra: Any) -> types.Tool:
    schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": True}
    return types.Tool(name=name, description=description, input_schema=schema, **extra)


_READ_ONLY = types.ToolAnnotations(read_only_hint=True)
_PAGED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "offset": {"type": "integer", "minimum": 0},
    },
    "required": ["limit"],
    "additionalProperties": True,
}


def _paged_tool(name: str, description: str, **extra: Any) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        input_schema=_PAGED_SCHEMA,
        annotations=_READ_ONLY,
        **extra,
    )


def _page(args: dict[str, Any] | None, *, n: int = 450) -> dict[str, Any]:
    total = _int_arg(args, "n", n)
    limit = _int_arg(args, "limit", 50)
    offset = _int_arg(args, "offset", 0)
    items = rows_payload(total)[offset : offset + limit]
    has_more = offset + len(items) < total
    return {
        "items": items,
        "returned": len(items),
        "has_more": has_more,
        "next_offset": offset + len(items) if has_more else None,
        "limit": limit,
        "offset": offset,
    }


def fake_tools() -> list[types.Tool]:
    return [
        _tool("rows", "n homogeneous objects under an 'items' key"),
        _tool("bare_rows", "a bare JSON array of objects"),
        _tool("mixed", "varying key sets; the 'v' column changes type at row 300"),
        _tool("nested", "objects containing nested objects and arrays"),
        _tool("wide", "k top-level keys, all equally present"),
        _tool("scalars", "a bare array of numbers"),
        _tool("just_text", "non-JSON prose"),
        _tool("one_object", "a single JSON object"),
        _tool("empty", "an empty items array"),
        _tool("boom", "returns isError"),
        _tool("picture", "returns an image content block"),
        _tool("two_arrays", "two qualifying arrays: rows (20) and facets (100)"),
        _tool("mixed_elements", "a list of 9 objects and 1 scalar"),
        _tool("structured_only", "data in structuredContent, prose in content"),
        _tool("both_channels", "both channels populated and disagreeing"),
        _tool("two_text_blocks", "two blocks each holding valid JSON"),
        _tool("edge_numbers", "int64 and 2^53 boundaries, non-finite floats"),
        _tool("rich_result", "carries result _meta and content annotations"),
        _tool("client_capabilities", "reports the capabilities the connected client advertised"),
        _tool("needs_input", "returns InputRequiredResult, completes on round two"),
        _tool(
            "destructive",
            "carries annotations a client uses for confirmation",
            annotations=types.ToolAnnotations(destructive_hint=True, title="Destructive"),
            title="Destructive Thing",
        ),
        _tool(
            "bad_schema",
            "declares an output schema and returns non-conforming output",
            output_schema={
                "type": "object",
                "properties": {"required_field": {"type": "string"}},
                "required": ["required_field"],
            },
        ),
        # Two distinct MCP tool names that collapse to the same string under
        # naive sanitizing. They must not share a mounted name or a table.
        _tool("hyphen-tool", "collides with hyphen_tool under naive slugging"),
        _tool("hyphen_tool", "collides with hyphen-tool under naive slugging"),
        _paged_tool(
            "paged", "offset-paginated rows: limit/offset in, items/has_more/next_offset out"
        ),
        _paged_tool("paged_stuck", "has_more is true but next_offset never advances"),
        _paged_tool("paged_no_next", "has_more is true but next_offset is null"),
        _paged_tool("paged_error_at", "pages at offset >= 200 return isError"),
        _paged_tool("paged_slow", "sleeps `delay` seconds per page"),
        _paged_tool("paged_bad_items", "`items` is not an array"),
        _paged_tool("paged_interactive", "page 2 asks for input"),
        _tool("paged_mutable", "paged, but without a read-only annotation"),
    ]


def page_two_tools() -> list[types.Tool]:
    return [_tool(PAGE_TWO_TOOL, "reachable only by following next_cursor")]


async def _call(
    context: ServerRequestContext[object],
    params: types.CallToolRequestParams,
) -> types.CallToolResult | types.InputRequiredResult:
    name = params.name
    args = params.arguments

    if name == "rows":
        return _ok({"items": rows_payload(_int_arg(args, "n", 400)), "next_cursor": None})
    if name == "bare_rows":
        return _ok(rows_payload(_int_arg(args, "n", 10)))
    if name == "mixed":
        n = _int_arg(args, "n", 301)
        items: list[dict[str, Any]] = [{"v": i, "always": i} for i in range(min(n, 300))]
        if n > 300:
            items.append({"v": "oops", "always": 300, "only_here": True})
        return _ok({"items": items})
    if name == "nested":
        return _ok(
            {
                "items": [
                    {"id": i, "meta": {"k": i, "deep": {"deeper": i}}, "tags": [f"t{i}", "x"]}
                    for i in range(5)
                ]
            }
        )
    if name == "wide":
        k = _int_arg(args, "k", 200)
        return _ok({"items": [{f"k{j:03d}": j for j in range(k)} for _ in range(3)]})
    if name == "scalars":
        return _ok([i * 1.5 for i in range(_int_arg(args, "n", 10))])
    if name == "just_text":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="no json here, only prose")]
        )
    if name == "one_object":
        return _ok({"id": 1, "name": "solo", "nested": {"a": 1}})
    if name == "empty":
        return _ok({"items": []})
    if name == "boom":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="downstream says no")],
            is_error=True,
            _meta={"vendor.example/errorCode": "E_NOPE"},
        )
    if name == "picture":
        return types.CallToolResult(
            content=[
                types.ImageContent(
                    type="image",
                    data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
                    mime_type="image/png",
                )
            ],
            _meta={"vendor.example/assetId": "img-7"},
        )
    if name == "two_arrays":
        return _ok(
            {
                "rows": [{"id": i, "amount": i * 10} for i in range(20)],
                "facets": [{"bucket": f"b{i}", "count": i} for i in range(100)],
            }
        )
    if name == "mixed_elements":
        return _ok({"items": [*({"id": i} for i in range(9)), 42]})
    if name == "structured_only":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Found 3 records for your query.")],
            structured_content={"items": [{"id": i, "score": i * 2} for i in range(3)]},
        )
    if name == "both_channels":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"scores": [100, 200]}))],
            structured_content={"scores": [1, 2]},
        )
    if name == "two_text_blocks":
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=json.dumps({"a": 1})),
                types.TextContent(type="text", text=json.dumps({"b": 2})),
            ]
        )
    if name == "edge_numbers":
        return _ok(
            {
                "items": [
                    {"big": 2**63 - 1, "huge": 2**64 + 1, "mid": 9007199254740993, "f": 0.5},
                    {"big": 0, "huge": 0, "mid": 1, "f": math.inf},
                ]
            }
        )
    if name == "rich_result":
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text="not json",
                    annotations=types.Annotations(audience=["user"], priority=0.7),
                    _meta={"vendor.example/trace": "abc123"},
                )
            ],
            _meta={"vendor.example/requestId": "req-42"},
        )
    if name == "client_capabilities":
        capabilities = context.session.client_capabilities
        return _ok(
            {
                "sampling": capabilities is not None and capabilities.sampling is not None,
                "elicitation": capabilities is not None
                and getattr(capabilities, "elicitation", None) is not None,
            }
        )
    if name == "destructive":
        return _ok({"items": [{"id": 1}]})
    if name == "bad_schema":
        # Declares `required_field` and does not return it, which makes the
        # client's validator raise rather than hand back a result.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="{}")],
            structured_content={"wrong_field": "nope"},
        )
    if name in {"hyphen-tool", "hyphen_tool"}:
        return _ok({"items": [{"which": name}]})
    if name == PAGE_TWO_TOOL:
        return _ok({"items": [{"page": 2}]})
    if name in {"paged", "paged_mutable"}:
        return _ok(_page(args))
    if name == "paged_stuck":
        return _ok({**_page(args), "has_more": True, "next_offset": 0})
    if name == "paged_no_next":
        return _ok({**_page(args), "has_more": True, "next_offset": None})
    if name == "paged_error_at":
        if _int_arg(args, "offset", 0) >= 200:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="page unavailable")], is_error=True
            )
        return _ok(_page(args))
    if name == "paged_slow":
        await anyio.sleep(float(_int_arg(args, "delay_ms", 300)) / 1000)
        return _ok(_page(args))
    if name == "paged_bad_items":
        return _ok({**_page(args), "items": "not a list"})
    if name == "paged_interactive":
        if _int_arg(args, "offset", 0) > 0 and params.request_state is None:
            return types.InputRequiredResult(
                input_requests={
                    "pick": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Continue?",
                            requested_schema={"type": "object", "properties": {}},
                        )
                    )
                },
                request_state="awaiting",
            )
        return _ok(_page(args))
    if name == "needs_input":
        if params.request_state is None:
            return types.InputRequiredResult(
                input_requests={
                    "pick": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Pick a number",
                            requested_schema={
                                "type": "object",
                                "properties": {"n": {"type": "integer"}},
                                "required": ["n"],
                            },
                        )
                    )
                },
                request_state="awaiting-pick",
            )
        answered = params.input_responses or {}
        return _ok(
            {"items": [{"round": 2, "state": params.request_state, "answers": len(answered)}]}
        )

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"unknown tool {name}")],
        is_error=True,
    )


async def _list(
    context: ServerRequestContext[object],
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    cursor = params.cursor if params is not None else None
    if cursor == PAGE_TWO_CURSOR:
        return types.ListToolsResult(tools=page_two_tools())
    return types.ListToolsResult(tools=fake_tools(), next_cursor=PAGE_TWO_CURSOR)


def build_fake_server() -> Server[object]:
    return Server("fake-downstream", version="0.1.0", on_list_tools=_list, on_call_tool=_call)
