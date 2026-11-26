"""The four downstream failure classes (spec 8).

Revision 2 of the spec promised that downstream errors are returned verbatim.
That is only achievable for one of the four classes. When a downstream tool
declares an `outputSchema` and its result does not conform, the MCP SDK raises
inside `call_tool` and there is no result object left to forward.
"""

from dataclasses import dataclass
from enum import StrEnum

from mcp import types


class FailureClass(StrEnum):
    TOOL_ERROR = "tool_error"
    """Downstream returned `isError: true`. The only class forwarded verbatim."""

    OUTPUT_SCHEMA = "output_schema"
    """Downstream declared an `outputSchema` and its result did not conform."""

    PROTOCOL = "protocol"
    """JSON-RPC error from downstream. Not an `isError` result."""

    TRANSPORT = "transport"
    """The downstream process died or its stream closed."""


@dataclass(slots=True)
class DownstreamError(Exception):
    """A downstream call that produced no forwardable result."""

    failure_class: FailureClass
    tool: str
    message: str

    def __str__(self) -> str:
        return f"{self.failure_class}: {self.tool}: {self.message}"


def error_result(failure: DownstreamError) -> types.CallToolResult:
    """Sluice's own error result, for the classes that cannot be forwarded."""
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"sluice: downstream call failed ({failure.failure_class}).\n"
                    f"tool: {failure.tool}\n"
                    f"{failure.message}"
                ),
            )
        ],
        is_error=True,
    )
