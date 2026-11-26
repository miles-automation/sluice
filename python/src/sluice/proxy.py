"""The downstream proxy: session, paginated listing, call forwarding, round-trip relay."""

import contextlib
import copy
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Any, Self

import anyio
from mcp import Client, ClientSession, MCPError, StdioServerParameters, types
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from sluice.config import Config, ConfigError, PaginationConfig
from sluice.errors import DownstreamError, FailureClass
from sluice.naming import NameCollisionError, assert_injective, collection_name, mounted_name
from sluice.paginate import FetchOutcome, fetch_all

logger = logging.getLogger(__name__)

HANDLE_NOTE = (
    "Results from this tool are stored in a session database. You receive a preview "
    "plus a table name; use the `query` tool to run SQL over the full result."
)

COLLECTION_NOTE = (
    "Fetches every page of `{tool}` (up to configured limits) and records ALL rows in ONE "
    "table. Pass the tool's filters only: Sluice supplies `{limit}` and `{offset}`. An "
    "`{offset}` may be given to resume a partial fetch. The handle states whether the fetch "
    "was complete or partial and why it stopped."
)

COLLECTION_CROSS_REFERENCE = (
    "To fetch every page of this tool at once into one table, call `{collection}` instead."
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


@dataclass(frozen=True, slots=True)
class MountedCollection:
    mounted: str
    server: str
    original: types.Tool
    exposed: types.Tool
    config: PaginationConfig


def expose_collection(server: str, tool: types.Tool, config: PaginationConfig) -> MountedCollection:
    mounted = collection_name(server, tool.name)
    schema: dict[str, Any] = (
        copy.deepcopy(tool.input_schema)
        if isinstance(tool.input_schema, dict)
        else {"type": "object", "properties": {}}
    )
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop(config.limit_arg, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name != config.limit_arg]
    note = COLLECTION_NOTE.format(tool=tool.name, limit=config.limit_arg, offset=config.offset_arg)
    description = tool.description or ""
    exposed = tool.model_copy(
        update={
            "name": mounted,
            "description": f"{description}\n\n{note}\n\n{HANDLE_NOTE}".strip(),
            "input_schema": schema,
            "output_schema": None,
        }
    )
    return MountedCollection(
        mounted=mounted, server=server, original=tool, exposed=exposed, config=config
    )


def _read_only(tool: types.Tool) -> bool:
    return tool.annotations is not None and tool.annotations.read_only_hint is True


class Proxy:
    """Holds the downstream session for the process lifetime."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Client | None = None
        self._session: ClientSession | None = None
        self._protocol_version: str | None = None
        self._tools: dict[str, MountedTool] = {}
        self._collections: dict[str, MountedCollection] = {}
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
        self._collections = self._mount_collections(server, tools, mounted)
        logger.info(
            "mounted %d downstream tools and %d collections", len(mounted), len(self._collections)
        )

    def _mount_collections(
        self, server: str, tools: list[types.Tool], mounted: dict[str, MountedTool]
    ) -> dict[str, MountedCollection]:
        by_name = {tool.name: tool for tool in tools}
        collections: dict[str, MountedCollection] = {}
        for tool_name, pagination in self._config.pagination.items():
            original = by_name.get(tool_name)
            if original is None:
                raise ConfigError(
                    f"[pagination.{tool_name}] names a tool the downstream did not list"
                )
            if not _read_only(original):
                raise ConfigError(
                    f"[pagination.{tool_name}] refused: the downstream tool is not annotated "
                    "readOnlyHint=true, and Sluice only fetches read-only tools automatically"
                )
            entry = expose_collection(server, original, pagination)
            if entry.mounted in mounted or entry.mounted in collections:
                raise NameCollisionError(
                    f"collection for {server}/{tool_name!r} collides with an existing mounted name "
                    f"{entry.mounted!r}"
                )
            collections[entry.mounted] = entry
            single = mounted[mounted_name(server, tool_name)]
            mounted[single.mounted] = replace(
                single,
                exposed=single.exposed.model_copy(
                    update={
                        "description": (
                            f"{single.exposed.description}\n\n"
                            f"{COLLECTION_CROSS_REFERENCE.format(collection=entry.mounted)}"
                        )
                    }
                ),
            )
        return collections

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
        return [
            *(entry.exposed for entry in self._tools.values()),
            *(entry.exposed for entry in self._collections.values()),
        ]

    def resolve(self, mounted: str) -> MountedTool | None:
        return self._tools.get(mounted)

    def resolve_collection(self, mounted: str) -> MountedCollection | None:
        return self._collections.get(mounted)

    async def fetch_collection(
        self, entry: MountedCollection, arguments: dict[str, Any] | None
    ) -> FetchOutcome:
        async def page(
            page_arguments: dict[str, Any],
        ) -> types.CallToolResult | types.InputRequiredResult:
            return await self._call_original(entry.original.name, page_arguments)

        return await fetch_all(page, entry.config, arguments)

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
        return await self._call_original(
            entry.original.name,
            arguments,
            input_responses=input_responses,
            request_state=request_state,
        )

    async def _call_original(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        if not self._healthy:
            raise DownstreamError(
                FailureClass.TRANSPORT,
                name,
                "downstream session is unhealthy after an earlier transport failure",
            )
        try:
            result = await self.session.call_tool(
                name,
                arguments,
                input_responses=input_responses,
                request_state=request_state,
                # Round trips come back to us rather than being answered inside
                # the SDK, which is what lets us relay them upstream.
                allow_input_required=True,
            )
        except MCPError as exc:
            raise DownstreamError(FailureClass.PROTOCOL, name, str(exc)) from exc
        except RuntimeError as exc:
            message = str(exc)
            if any(marker in message.lower() for marker in _OUTPUT_SCHEMA_MARKERS):
                raise DownstreamError(FailureClass.OUTPUT_SCHEMA, name, message) from exc
            raise
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._healthy = False
            raise DownstreamError(
                FailureClass.TRANSPORT, name, f"{type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(result, types.CallToolResult | types.InputRequiredResult):
            return result
        raise DownstreamError(
            FailureClass.PROTOCOL, name, f"unexpected result type {type(result).__name__}"
        )


@contextlib.asynccontextmanager
async def connect(config: Config):  # type: ignore[no-untyped-def]
    """Convenience wrapper owning the exit stack."""
    async with AsyncExitStack() as stack:
        yield await Proxy.start(config, stack)
