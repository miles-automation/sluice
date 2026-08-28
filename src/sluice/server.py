"""The MCP server Sluice exposes upstream."""

import logging

from mcp import types
from mcp.server import NotificationOptions, Server, ServerRequestContext

from sluice import __version__
from sluice.errors import DownstreamError, FailureClass, error_result
from sluice.proxy import MODERN_VERSIONS, Proxy

logger = logging.getLogger(__name__)

SERVER_NAME = "sluice"

INSTRUCTIONS = (
    "Tool results are materialized into a scratch database. Each call returns a "
    "short preview plus a table name; run SQL over the full result with `query`."
)


def build_server(proxy: Proxy) -> Server[object]:
    async def on_list_tools(
        context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        # The catalog is static, captured at startup (spec 10). It is small
        # enough to serve in one page, so no cursor is returned.
        return types.ListToolsResult(tools=proxy.mounted_tools())

    async def on_call_tool(
        context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
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
            return result

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
