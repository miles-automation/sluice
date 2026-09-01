"""The downstream proxy: session, paginated listing, call forwarding, round-trip relay."""

import contextlib
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Self

import anyio
from mcp import Client, ClientSession, MCPError, StdioServerParameters, types
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from sluice.config import Config
from sluice.errors import DownstreamError, FailureClass
from sluice.naming import assert_injective, mounted_name

logger = logging.getLogger(__name__)

HANDLE_NOTE = (
    "Results from this tool are stored in a session database. You receive a preview "
    "plus a table name; use the `query` tool to run SQL over the full result."
)

MAX_LIST_PAGES = 1000
"""Runaway guard. A server that never returns a null cursor is broken, and
looping forever on it would hang startup with no diagnostic."""

MODERN_VERSIONS = frozenset(MODERN_PROTOCOL_VERSIONS)
"""Protocol versions where `tools/call` may return `InputRequiredResult`.

Reaching one is not automatic. The `initialize` handshake tops out at
2025-11-25; `2026-07-28` is negotiated by a `server/discover` probe, which
`mcp.Client` performs and a bare `ClientSession.initialize()` does not. A proxy
built on the handshake alone silently loses round-trip support, and the failure
looks like a serialization error rather than a version mismatch.
"""

# The SDK signals output-schema validation failure with a bare RuntimeError.
# Classifying on the message is fragile, so tests/test_engine_contract.py pins
# these substrings against the installed SDK; if that test fails, fix this map.
_OUTPUT_SCHEMA_MARKERS = ("output schema", "structured content")


@dataclass(frozen=True, slots=True)
class MountedTool:
    """A downstream tool and the definition Sluice exposes for it."""

    mounted: str
    server: str
    original: types.Tool
    exposed: types.Tool


def expose(server: str, tool: types.Tool) -> MountedTool:
    """Clone the whole downstream tool, mutating only three fields (FR-3).

    Cloning rather than reconstructing field by field is deliberate. A tool
    object carries `title`, `icons`, `annotations`, `_meta`, and fields this
    version has never heard of. Dropping `annotations.destructive_hint` would
    turn off the signal a client uses to ask the user for confirmation, which is
    a safety regression produced by a refactor that looks harmless.
    """
    mounted = mounted_name(server, tool.name)
    description = tool.description or ""
    exposed = tool.model_copy(
        update={
            "name": mounted,
            "description": f"{description}\n\n{HANDLE_NOTE}".strip(),
            # Removed, not overridden: a tool declaring an output schema requires
            # conforming structured content, and Sluice's handle does not conform
            # (spec 4.3).
            "output_schema": None,
        }
    )
    return MountedTool(mounted=mounted, server=server, original=tool, exposed=exposed)


class Proxy:
    """Holds the downstream session for the process lifetime."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Client | None = None
        self._session: ClientSession | None = None
        self._protocol_version: str | None = None
        self._tools: dict[str, MountedTool] = {}
        self._healthy = True

    @classmethod
    async def start(cls, config: Config, stack: AsyncExitStack) -> Self:
        proxy = cls(config)
        await proxy._connect(stack)
        return proxy

    async def _connect(self, stack: AsyncExitStack) -> None:
        server = self._config.server
        assert server.command is not None  # config rejects url-only in v0
        params = StdioServerParameters(
            command=server.command,
            args=server.args,
            env=server.env or None,
            cwd=server.cwd,
        )
        try:
            # `Client` rather than raw stdio_client + ClientSession, for one
            # reason: it probes `server/discover` and so can negotiate
            # 2026-07-28, where `tools/call` may return `InputRequiredResult`.
            # The bare handshake tops out at 2025-11-25 and round trips do not
            # exist there.
            #
            # No sampling_callback and no elicitation_callback, deliberately.
            # Passing either would advertise the capability downstream and make
            # Sluice answer prompts addressed to the real client and its human
            # (spec 11). Calls go through `client.session` below for the same
            # reason: `Client.call_tool` resolves round trips internally.
            client = await stack.enter_async_context(Client(params))
        except Exception as exc:
            raise DownstreamError(
                FailureClass.TRANSPORT, server.name, f"could not start downstream: {exc}"
            ) from exc
        self._client = client
        self._session = client.session
        self._protocol_version = client.protocol_version
        if self._protocol_version not in MODERN_VERSIONS:
            logger.warning(
                "downstream negotiated %s; interactive tool calls are unavailable below %s",
                self._protocol_version,
                sorted(MODERN_VERSIONS)[0],
            )
        await self.refresh_tools()

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("proxy is not connected")
        return self._session

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    @property
    def supports_round_trips(self) -> bool:
        return self._protocol_version in MODERN_VERSIONS

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def refresh_tools(self) -> None:
        """Capture the static tool catalog (spec 10)."""
        tools = await self._list_all_tools()
        server = self._config.server.name
        # Fails loudly rather than letting the second tool overwrite the first
        # in the dict, which would leave the agent calling one tool and reaching
        # another.
        assert_injective([(server, tool.name) for tool in tools])
        mounted: dict[str, MountedTool] = {}
        for tool in tools:
            entry = expose(server, tool)
            mounted[entry.mounted] = entry
        self._tools = mounted
        logger.info("mounted %d downstream tools", len(mounted))

    async def _list_all_tools(self) -> list[types.Tool]:
        """Follow `next_cursor` to completion.

        `ClientSession.list_tools()` returns a single page and does not paginate
        for you (verified against mcp 2.1.1). A tool that appears only on page 2
        would otherwise never be mounted, and every single-page test would still
        pass.
        """
        collected: list[types.Tool] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
            result = await self.session.list_tools(params=params)
            collected.extend(result.tools)
            next_cursor = result.next_cursor
            # `None` terminates. An empty string does not: it is a valid opaque
            # cursor value, so testing truthiness here would drop the last page.
            if next_cursor is None:
                return collected
            if next_cursor in seen:
                logger.warning("downstream repeated cursor %r; stopping", next_cursor)
                return collected
            seen.add(next_cursor)
            cursor = next_cursor
        logger.warning("downstream listing exceeded %d pages; stopping", MAX_LIST_PAGES)
        return collected

    def mounted_tools(self) -> list[types.Tool]:
        return [entry.exposed for entry in self._tools.values()]

    def resolve(self, mounted: str) -> MountedTool | None:
        return self._tools.get(mounted)

    async def call(
        self,
        mounted: str,
        arguments: dict[str, Any] | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """Forward a call, relaying round trips untouched (FR-5)."""
        entry = self.resolve(mounted)
        if entry is None:
            raise DownstreamError(FailureClass.PROTOCOL, mounted, f"no such tool: {mounted}")
        if not self._healthy:
            raise DownstreamError(
                FailureClass.TRANSPORT,
                entry.original.name,
                "downstream session is unhealthy after an earlier transport failure",
            )
        try:
            result = await self.session.call_tool(
                entry.original.name,
                arguments,
                input_responses=input_responses,
                request_state=request_state,
                # Round trips come back to us rather than being answered inside
                # the SDK, which is what lets us relay them upstream.
                allow_input_required=True,
            )
        except MCPError as exc:
            raise DownstreamError(FailureClass.PROTOCOL, entry.original.name, str(exc)) from exc
        except RuntimeError as exc:
            message = str(exc)
            if any(marker in message.lower() for marker in _OUTPUT_SCHEMA_MARKERS):
                raise DownstreamError(
                    FailureClass.OUTPUT_SCHEMA, entry.original.name, message
                ) from exc
            raise
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._healthy = False
            raise DownstreamError(
                FailureClass.TRANSPORT, entry.original.name, f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(result, types.CallToolResult | types.InputRequiredResult):
            return result
        raise DownstreamError(
            FailureClass.PROTOCOL,
            entry.original.name,
            f"unexpected result type {type(result).__name__}",
        )


@contextlib.asynccontextmanager
async def connect(config: Config):  # type: ignore[no-untyped-def]
    """Convenience wrapper owning the exit stack."""
    async with AsyncExitStack() as stack:
        yield await Proxy.start(config, stack)
