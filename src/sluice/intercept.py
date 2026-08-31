"""Turning a downstream result into what the agent sees (spec 4, 5.1, 8)."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import anyio
from mcp import types

from sluice import handle as handle_render
from sluice import naming, scope, shape
from sluice import payload as payload_select
from sluice.config import Limits
from sluice.infer import ColumnSpec, ColumnType, infer_column
from sluice.models import (
    CallRecord,
    ColumnRef,
    Handle,
    Passthrough,
    PayloadChannel,
    SelectedPayload,
    TablePlan,
    TableRef,
)
from sluice.store import Store

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Interceptor:
    """Records every call and replaces eligible results with a handle."""

    def __init__(self, store: Store, limits: Limits, *, query_available: bool = False) -> None:
        self._store = store
        self._limits = limits
        self._query_available = query_available
        # Admission gate over the whole pipeline, not just the write. Peak
        # memory is a multiple of payload size, and parsing and projection both
        # happen before the write lock is taken (spec 7).
        self._admission = anyio.Semaphore(limits.max_concurrent_materializations)

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
        # The SDK has already decoded structuredContent before this method is
        # called.  Everything Sluice does next can multiply that memory (text
        # parsing, projection, plans, DuckDB insertion, and handle rendering),
        # so admission starts at the outermost interception boundary.
        async with self._admission:
            return await self._intercept(
                server=server,
                tool=tool,
                mounted=mounted,
                arguments=arguments,
                result=result,
                meta=meta,
                started_at=started_at,
            )

    async def _intercept(
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
        seq = await self._store.next_call_seq(mounted)
        retention_seq = await self._store.next_retention_seq()
        call_id = str(uuid4())

        reason = payload_select.passthrough_reason(result, self._limits.max_payload_bytes)

        if reason is not None:
            # Passthrough is intentionally a metadata-only retention path. The
            # original result is returned below, while no error/binary/oversize
            # text is copied into any envelope payload column.
            selected = payload_select.metadata_only(result)
        else:
            selected = payload_select.select(result)

        plans: list[TablePlan] = []
        tables: list[TableRef] = []
        flat_reason: str | None = None
        preview_rows: list[object] = []
        if reason is None:
            plans, tables, flat_reason, preview_rows = await self._plan(mounted, scope_id, selected)

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
            retention_seq=retention_seq,
            flat_tables=[table.name for table in tables],
            source_paths=[table.source_path for table in tables],
            flat_reason=flat_reason,
        )

        try:
            retained = await self._store.commit_call(record, plans)
            if not retained:
                # The call itself exceeded the session retention budget. The
                # envelope remains as metadata, but no handle may advertise
                # tables that were immediately evicted.
                tables = []
                record = replace(
                    record,
                    flat_tables=[],
                    source_paths=[],
                    flat_reason="retention_budget_exceeded",
                )
        except Exception as exc:
            # The tables and the envelope go in together, so a failure here has
            # left the database untouched. Retry with no tables so the call is
            # still recorded, and say why the tables are missing.
            logger.exception("commit failed for %s; retrying envelope-only", tool)
            tables = []
            # flat_tables and source_paths are parallel arrays describing the
            # same tables, so clearing one without the other leaves an envelope
            # claiming paths for tables that do not exist.
            degraded = replace(
                record,
                flat_tables=[],
                source_paths=[],
                flat_reason=f"load_failed: {type(exc).__name__}: {exc}",
            )
            try:
                retry_retained = await self._store.commit_call(degraded, [])
                if not retry_retained:
                    record = replace(
                        degraded,
                        flat_reason="retention_budget_exceeded",
                    )
                else:
                    record = degraded
            except Exception:
                # FR-8 is conditional on the envelope write. There is no
                # fallback journal, so hand back what the tool actually
                # returned rather than fail a call that worked.
                logger.exception("envelope write failed for %s; passing through", tool)
                return result
            record = degraded

        if reason is not None:
            return self._passthrough(result, reason, selected)

        return handle_render.to_result(self._build_handle(record, selected, tables, preview_rows))

    def _passthrough(
        self,
        result: types.CallToolResult,
        reason: Passthrough,
        selected: SelectedPayload,
    ) -> types.CallToolResult:
        # FR-12 and FR-13 preserve the downstream object byte-for-byte. The
        # oversize path adds its established size note while retaining no
        # payload data in the envelope.
        if reason is Passthrough.OVERSIZE:
            note = handle_render.size_note(selected.byte_size, self._limits.max_payload_bytes)
            return result.model_copy(
                update={"content": [*result.content, types.TextContent(type="text", text=note)]}
            )
        return result

    async def _plan(
        self, mounted: str, scope_id: str, selected: SelectedPayload
    ) -> tuple[list[TablePlan], list[TableRef], str | None, list[object]]:
        """Compute every table without touching the database.

        Planning is separated from writing so the write can be one transaction.
        The caller holds the admission slot across selection, planning, commit,
        and handle construction. A failure here is reported on the envelope
        row; it never fails the tool call (FR-10).
        """
        if selected.channel is PayloadChannel.NONE:
            return [], [], "not_json: no channel parsed as JSON", []
        try:
            return await self._build_plans(mounted, scope_id, selected)
        except Exception as exc:
            logger.exception("planning failed for %s", mounted)
            return [], [], f"load_failed: {type(exc).__name__}: {exc}", []

    async def _build_plans(
        self, mounted: str, scope_id: str, selected: SelectedPayload
    ) -> tuple[list[TablePlan], list[TableRef], str | None, list[object]]:
        extraction = shape.extract(selected.value)
        if not extraction.row_sets:
            return [], [], extraction.reason, []
        plans: list[TablePlan] = []
        tables: list[TableRef] = []
        preview_rows: list[object] = []
        for row_set in extraction.row_sets:
            if not preview_rows:
                preview_rows = payload_select.bounded_preview_rows(
                    row_set.rows, self._limits.preview_rows, self._limits.preview_bytes
                )
            projection = shape.project(row_set.rows, self._limits.max_columns)
            stored_to_source = {v: k for k, v in projection.renamed.items()}
            specs: list[ColumnSpec] = []
            for column in projection.columns:
                values = [record.get(column) for record in projection.records]
                if column == shape.EXTRA_COLUMN:
                    column_type, exact = ColumnType.JSON, False
                else:
                    column_type, exact = infer_column(values)
                specs.append(
                    ColumnSpec(
                        name=column,
                        type=column_type,
                        exact=exact,
                        renamed_from=stored_to_source.get(column),
                    )
                )
            seq = await self._store.next_table_seq(mounted)
            name = naming.table_name(mounted, scope_id, seq)
            plans.append(
                TablePlan(
                    name=name,
                    source_path=row_set.source_path,
                    columns=specs,
                    records=projection.records,
                )
            )
            tables.append(
                TableRef(
                    name=name,
                    source_path=row_set.source_path,
                    row_count=len(projection.records),
                    columns=[
                        ColumnRef(
                            name=spec.name,
                            type=str(spec.type),
                            exact=spec.exact,
                            renamed_from=spec.renamed_from,
                        )
                        for spec in specs
                    ],
                )
            )
        return plans, tables, None, preview_rows

    def _build_handle(
        self,
        record: CallRecord,
        selected: SelectedPayload,
        tables: list[TableRef],
        preview_rows: list[object],
    ) -> Handle:
        preview, complete = payload_select.render_preview(selected, self._limits.preview_bytes)
        shown: int | None = None
        total: int | None = None
        if not complete and tables:
            preview, shown = payload_select.render_row_preview(
                preview_rows, len(preview_rows), self._limits.preview_bytes
            )
            total = tables[0].row_count
        return Handle(
            call_id=record.call_id,
            scope_id=record.scope_id,
            channel=selected.channel,
            conflict=selected.conflict,
            byte_size=selected.byte_size,
            preview=preview,
            preview_complete=complete,
            preview_rows=shown,
            total_rows=total,
            tables=tables,
            flat_reason=record.flat_reason,
            query_available=self._query_available,
        )
