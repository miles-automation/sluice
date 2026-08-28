"""Turning a downstream result into what the agent sees (spec 4, 5.1, 8)."""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from mcp import types

from sluice import handle as handle_render
from sluice import payload as payload_select
from sluice import scope
from sluice.config import Limits
from sluice.models import CallRecord, Handle, Passthrough, PayloadChannel, SelectedPayload
from sluice.store import Store

logger = logging.getLogger(__name__)

FLATTENING_PENDING = "flattening not implemented yet (plan M3)"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Interceptor:
    """Records every call and replaces eligible results with a handle."""

    def __init__(self, store: Store, limits: Limits, *, query_available: bool = False) -> None:
        self._store = store
        self._limits = limits
        self._query_available = query_available

    async def intercept(
        self,
        *,
        server: str,
        tool: str,
        mounted: str,
        arguments: dict[str, object] | None,
        result: types.CallToolResult,
        meta: object | None,
        started_at: datetime,
    ) -> types.CallToolResult:
        ended_at = _now()
        scope_id, _ = scope.derive(meta)
        seq = await self._store.next_seq(mounted)
        call_id = str(uuid4())

        reason = payload_select.passthrough_reason(result, self._limits.max_payload_bytes)

        if reason is Passthrough.OVERSIZE:
            # No parse: the payload columns stay NULL and only the sizes are
            # recorded (spec 3.1). Revision 2 required both "no parse" and a
            # populated `result`, which was self-contradictory.
            selected = SelectedPayload(
                channel=PayloadChannel.NONE,
                value=None,
                text=None,
                blocks=payload_select.describe_blocks(result),
                byte_size=payload_select.candidate_size(result),
                wire_bytes=payload_select.wire_bytes(result),
            )
        else:
            selected = payload_select.select(result)

        record = CallRecord(
            call_id=call_id,
            scope_id=scope_id,
            seq=seq,
            server=server,
            tool=tool,
            args=dict(arguments) if arguments else None,
            payload=selected,
            is_error=bool(result.is_error),
            failure_class="tool_error" if result.is_error else None,
            content_kinds=payload_select.content_kinds(result),
            started_at=started_at,
            ended_at=ended_at,
            flat_reason=None if reason else FLATTENING_PENDING,
        )

        try:
            await self._store.record(record)
        except Exception:
            # FR-8 is conditional on this write. There is no fallback journal, so
            # the honest response is to log and hand back what the tool actually
            # returned rather than fail a call that worked.
            logger.exception("envelope write failed for %s; passing the result through", tool)
            return result

        if reason is not None:
            return self._passthrough(result, reason, selected)

        return handle_render.to_result(self._build_handle(record, selected))

    def _passthrough(
        self,
        result: types.CallToolResult,
        reason: Passthrough,
        selected: SelectedPayload,
    ) -> types.CallToolResult:
        if reason is not Passthrough.OVERSIZE:
            # FR-12 and FR-13: errors and non-text content go back untouched.
            # An error is diagnostic and mangling it makes debugging worse;
            # binary content has nothing to flatten.
            return result
        note = handle_render.size_note(selected.byte_size, self._limits.max_payload_bytes)
        return result.model_copy(
            update={"content": [*result.content, types.TextContent(type="text", text=note)]}
        )

    def _build_handle(self, record: CallRecord, selected: SelectedPayload) -> Handle:
        preview, complete = payload_select.render_preview(selected, self._limits.preview_bytes)
        flat_reason = record.flat_reason
        if selected.channel is PayloadChannel.NONE:
            flat_reason = "not_json"
        return Handle(
            call_id=record.call_id,
            scope_id=record.scope_id,
            channel=selected.channel,
            conflict=selected.conflict,
            byte_size=selected.byte_size,
            preview=preview,
            preview_complete=complete,
            tables=[],
            flat_reason=flat_reason,
            query_available=self._query_available,
        )
