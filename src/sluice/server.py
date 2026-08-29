"""The MCP server Sluice exposes upstream."""

import logging
from datetime import UTC, datetime

from mcp import types
from mcp.server import NotificationOptions, Server, ServerRequestContext

from sluice import __version__
from sluice.errors import DownstreamError, FailureClass, error_result
from sluice.gate import QueryRejectedError
from sluice.intercept import Interceptor
from sluice.proxy import MODERN_VERSIONS, Proxy
from sluice.query import QUERY_DESCRIPTION, QUERY_SCHEMA, QUERY_TOOL_NAME, QueryTool

logger = logging.getLogger(__name__)

SERVER_NAME = "sluice"

INSTRUCTIONS = (
    "Tool results are materialized into a scratch database. Each call returns a "
    "short preview plus a table name; run SQL over the full result with `query`."
)


def query_tool_definition() -> types.Tool:
    return types.Tool(
        name=QUERY_TOOL_NAME,
        title="Query materialized results",
        description=QUERY_DESCRIPTION,
        input_schema=QUERY_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False),
    )


def build_server(
    proxy: Proxy,
    interceptor: Interceptor | None = None,
    query: QueryTool | None = None,
) -> Server[object]:
    async def on_list_tools(
        context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        # The catalog is static, captured at startup (spec 10). It is small
        # enough to serve in one page, so no cursor is returned.
        tools = proxy.mounted_tools()
        if query is not None:
            tools = [*tools, query_tool_definition()]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        if params.name == QUERY_TOOL_NAME:
            if query is None:
                return _text_error("the query tool is not available")
            return await _run_query(query, params)

        started_at = datetime.now(UTC).replace(tzinfo=None)
        try:
            result = await proxy.call(
                params.name,
                params.arguments,
                # Relayed untouched. `request_state` is opaque and must be
                # neither parsed nor rewritten (spec 11).
                input_responses=params.input_responses,
                request_state=params.request_state,
            )
        except DownstreamError as failure:
            logger.warning("downstream failure: %s", failure)
            return error_result(failure)
        else:
            if isinstance(result, types.InputRequiredResult) and (
                context.protocol_version not in MODERN_VERSIONS
            ):
                # Downstream asked for input, but the upstream client negotiated
                # a version where `tools/call` cannot carry that result. Handing
                # it up anyway fails inside the serializer with a validation
                # error that names neither the tool nor the version, so say what
                # actually happened instead.
                return error_result(
                    DownstreamError(
                        FailureClass.PROTOCOL,
                        params.name,
                        f"the tool requested input, which requires protocol "
                        f"{sorted(MODERN_VERSIONS)[0]}; this client negotiated "
                        f"{context.protocol_version}",
                    )
                )
            if interceptor is None or isinstance(result, types.InputRequiredResult):
                # An interim result is not a payload. Materialization runs only
                # on a final CallToolResult (spec 11).
                return result
            entry = proxy.resolve(params.name)
            return await interceptor.intercept(
                server=entry.server if entry else "unknown",
                tool=entry.original.name if entry else params.name,
                mounted=params.name,
                arguments=params.arguments,
                result=result,
                meta=params.meta,
                started_at=started_at,
            )

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def initialization_options(server: Server[object]) -> types.InitializeResult | object:
    """Static catalog, so `listChanged` is false (spec 10)."""
    return server.create_initialization_options(
        NotificationOptions(tools_changed=False),
    )


def _text_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"sluice: {message}")], is_error=True
    )


async def _run_query(query: QueryTool, params: types.CallToolRequestParams) -> types.CallToolResult:
    arguments = params.arguments or {}
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return _text_error("query needs a `sql` string")
    raw_max_rows = arguments.get("max_rows")
    if raw_max_rows is not None and (
        isinstance(raw_max_rows, bool) or not isinstance(raw_max_rows, int)
    ):
        return _text_error("max_rows must be an integer")
    try:
        rendered = await query.run(sql, raw_max_rows)
    except QueryRejectedError as exc:
        # An error, never an empty success. A rejected or failed query that came
        # back as a result set with no rows would read as "no matches".
        return _text_error(str(exc))
    return types.CallToolResult(content=[types.TextContent(type="text", text=rendered)])
