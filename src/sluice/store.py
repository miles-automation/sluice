"""The DuckDB scratch database: lockdown, envelope, sequence numbers."""

import json
import logging
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Self

import anyio
import duckdb

from sluice.config import SESSION_CALLS_DEFAULT, SESSION_RETENTION_DEFAULT, Limits
from sluice.gate import SCOPE_PATTERN
from sluice.infer import ColumnSpec, coerce
from sluice.models import CallRecord, SelectedPayload, TablePlan
from sluice.naming import quote_ident
from sluice.shape import CALL_COLUMN, ROW_COLUMN

logger = logging.getLogger(__name__)

MAX_FLAT_REASON_BYTES = 512


def bounded_flat_reason(reason: str | None) -> str | None:
    """Bound diagnostic text that may include a downstream-controlled value."""
    if reason is None:
        return None
    encoded = reason.encode("utf-8")
    if len(encoded) <= MAX_FLAT_REASON_BYTES:
        return reason
    cut = encoded[:MAX_FLAT_REASON_BYTES]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def retention_budget_reason(prior: str | None) -> str:
    reason = "retention_budget_exceeded"
    if prior:
        reason += f"; prior={prior}"
    return bounded_flat_reason(reason) or "retention_budget_exceeded"


LOCKDOWN_TEMPLATE: tuple[str, ...] = (
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    "SET max_memory = '{max_memory}'",
    # Must be last. Nothing above can be changed after this runs, including by
    # SQL the agent sends.
    "SET lock_configuration = true",
)

ENVELOPE_TABLE = "sluice_calls"
"""The physical envelope. Never queryable by the agent.

It holds `flat_tables` for every scope, so exposing it would hand any
conversation the table names of every other one and defeat spec 12 with a single
SELECT. Each scope gets a filtered view instead (`scope_view_name`), and the
gate's allowlist admits the views, never this."""


def scope_view_name(scope_id: str) -> str:
    return f"{ENVELOPE_TABLE}__{scope_id}"


ENVELOPE_DDL = f"""
CREATE TABLE {ENVELOPE_TABLE} (
    call_id           VARCHAR PRIMARY KEY,
    scope_id          VARCHAR,
    seq               BIGINT,
    server            VARCHAR,
    tool              VARCHAR,
    args              JSON,
    result            JSON,
    result_text       VARCHAR,
    result_blocks     JSON,
    result_structured JSON,
    source_channel    VARCHAR,
    channel_conflict  BOOLEAN,
    byte_size         BIGINT,
    wire_bytes        BIGINT,
    is_error          BOOLEAN,
    failure_class     VARCHAR,
    content_kinds     VARCHAR[],
    flat_tables       VARCHAR[],
    flat_reason       VARCHAR,
    source_paths      VARCHAR[],
    started_at        TIMESTAMP,
    ended_at          TIMESTAMP,
    duration_ms       BIGINT
)
"""

_INSERT = f"INSERT INTO {ENVELOPE_TABLE} VALUES ({', '.join(['?'] * 23)})"


def _json_or_none(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


@dataclass(frozen=True, slots=True)
class _RetainedCall:
    order: int
    size: int
    tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CallMetadata:
    order: int
    scope_id: str
    tables: tuple[str, ...]


class Store:
    """Owns the connection Sluice writes through.

    The lockdown is database-global, so it applies to Sluice's own SQL exactly as
    it applies to the agent's. That is why materialization cannot use `read_json`
    (spec 5.4). Do not relax it to fix a write.
    """

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        max_session_bytes: int = SESSION_RETENTION_DEFAULT,
        max_session_calls: int = SESSION_CALLS_DEFAULT,
    ) -> None:
        self._connection = connection
        self._max_session_bytes = max_session_bytes
        self._max_session_calls = max_session_calls
        self._lock = anyio.Lock()
        self._commit_condition = anyio.Condition()
        self._next_commit_seq = 1
        self._retry_seq: int | None = None
        self._abandoned_sequences: set[int] = set()
        self._call_sequences: dict[str, int] = {}
        self._table_sequences: dict[str, int] = {}
        self._retention_sequence = 0
        self._retained: dict[str, _RetainedCall] = {}
        self._metadata: dict[str, _CallMetadata] = {}
        self._retained_bytes = 0
        self._evicted_tables: set[str] = set()
        self._evicted_table_order: deque[str] = deque()
        self._evicted_table_limit = max_session_calls * 64
        # Exactly the objects the gate will admit. Nothing else in the catalog
        # is reachable, whether or not anyone thought to deny it by name.
        self._allowed_objects: set[str] = set()

    @classmethod
    def open(cls, limits: Limits) -> Self:
        connection = duckdb.connect(":memory:")
        for statement in LOCKDOWN_TEMPLATE:
            connection.execute(statement.format(max_memory=limits.duckdb_max_memory))
        # CREATE TABLE still works after the lockdown; only external access and
        # further configuration changes are closed off.
        connection.execute(ENVELOPE_DDL)
        return cls(connection, limits.max_session_bytes, limits.max_session_calls)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._connection

    @property
    def allowed_objects(self) -> set[str]:
        return set(self._allowed_objects)

    @property
    def retained_bytes(self) -> int:
        """Logical bytes retained by payload and table row representations."""
        return self._retained_bytes

    @property
    def retained_call_ids(self) -> set[str]:
        return set(self._retained)

    @property
    def evicted_tables(self) -> set[str]:
        """Table names removed by retention, for a precise stale-handle error."""
        return set(self._evicted_tables)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def next_call_seq(self, mounted: str) -> int:
        """Nth call to this tool. Recorded on the envelope row."""
        return await self._bump(self._call_sequences, mounted)

    async def next_table_seq(self, mounted: str) -> int:
        """Nth table created for this tool. Appears in the table name.

        Separate from the call counter because one call may produce several
        tables (spec 5.2), and a shared counter would make both numbers mean
        neither thing.
        """
        return await self._bump(self._table_sequences, mounted)

    async def next_retention_seq(self) -> int:
        """Assign a deterministic arrival order before payload work begins."""
        async with self._lock:
            self._retention_sequence += 1
            return self._retention_sequence

    async def _bump(self, counters: dict[str, int], key: str) -> int:
        async with self._lock:
            nxt = counters.get(key, 0) + 1
            counters[key] = nxt
            return nxt

    async def commit_call(self, record: CallRecord, plans: list[TablePlan]) -> bool:
        """Write the flat tables and the envelope row as one transaction.

        Atomicity matters here for a specific reason: the envelope is the only
        record that a table exists. Creating tables and then failing to write
        the envelope leaves tables nothing points at, and failing partway
        through several candidate tables leaves some of them. Either the call is
        fully recorded or the database is untouched.
        """
        # Records created by the interceptor carry a sequence assigned before
        # planning. Direct Store users receive a ticket here. Waiting makes
        # physical commit order independent of how long each plan took.
        direct_ticket = record.retention_seq <= 0
        if direct_ticket:
            record = replace(record, retention_seq=await self.next_retention_seq())

        sequence = record.retention_seq
        ready = False
        retry = False
        try:
            async with self._commit_condition:
                while sequence != self._next_commit_seq and sequence != self._retry_seq:
                    if sequence < self._next_commit_seq:
                        raise RuntimeError(f"retention ticket {sequence} is no longer active")
                    await self._commit_condition.wait()
                retry = sequence == self._retry_seq
                ready = True
            async with self._lock:
                outcome = await anyio.to_thread.run_sync(self._commit, record, plans)
        except BaseException as exc:
            # Cancellation remains active while unwinding, so cleanup must be
            # shielded. A caller cancelled while waiting abandons only its own
            # future ticket; it must not skip the call currently committing.
            with anyio.CancelScope(shield=True):
                if not ready:
                    await self.abandon_commit(sequence)
                else:
                    async with self._commit_condition:
                        # Interceptor commits retry one ordinary write failure
                        # with an envelope-only record. Direct Store callers
                        # have no such protocol, so release their ticket now.
                        if direct_ticket or retry or not isinstance(exc, Exception):
                            self._finish_commit(sequence)
                        else:
                            self._retry_seq = sequence
                            self._commit_condition.notify_all()
            raise
        # Do not let cancellation land between a successful database commit
        # and advancing the FIFO sequence.
        with anyio.CancelScope(shield=True):
            async with self._commit_condition:
                self._finish_commit(sequence)
        return outcome

    def _finish_commit(self, sequence: int) -> None:
        if self._retry_seq == sequence:
            self._retry_seq = None
        self._next_commit_seq = max(self._next_commit_seq, sequence + 1)
        self._drain_abandoned()
        self._commit_condition.notify_all()

    async def abandon_commit(self, sequence: int) -> None:
        """Release a ticket whose call failed or was cancelled before commit."""
        async with self._commit_condition:
            if sequence < self._next_commit_seq:
                return
            self._abandoned_sequences.add(sequence)
            if self._retry_seq == sequence:
                self._retry_seq = None
            self._drain_abandoned()
            self._commit_condition.notify_all()

    def _drain_abandoned(self) -> None:
        while self._next_commit_seq in self._abandoned_sequences:
            self._abandoned_sequences.remove(self._next_commit_seq)
            self._next_commit_seq += 1

    def _commit(self, record: CallRecord, plans: list[TablePlan]) -> bool:
        record = replace(record, flat_reason=bounded_flat_reason(record.flat_reason))
        size = self._retention_size(record, plans)
        order = record.retention_seq or self._retention_sequence + 1
        fits_budget = size <= self._max_session_bytes
        stored_record = record
        if not fits_budget:
            stored_record = replace(
                record,
                args=None,
                flat_tables=[],
                source_paths=[],
                flat_reason=retention_budget_reason(record.flat_reason),
                payload=self._metadata_payload(record.payload),
            )
        new_entry = _RetainedCall(
            order=order,
            size=size,
            tables=tuple(plan.name for plan in plans) if fits_budget else (),
        )
        retained = dict(self._retained)
        if fits_budget:
            retained[record.call_id] = new_entry
        metadata = dict(self._metadata)
        metadata[record.call_id] = _CallMetadata(
            order=order,
            scope_id=record.scope_id,
            tables=new_entry.tables,
        )
        self._connection.execute("BEGIN TRANSACTION")
        try:
            for plan in plans if fits_budget else []:
                self._create_flat(plan, record.call_id)
            self._insert_envelope(stored_record)
            view = self._ensure_scope_view(stored_record.scope_id)
            while self._retained_total(retained) > self._max_session_bytes:
                victim_id = min(
                    retained,
                    key=lambda call_id: (retained[call_id].order, call_id),
                )
                byte_victim = retained.pop(victim_id)
                self._evict(
                    victim_id,
                    byte_victim,
                    reason="retention_evicted",
                )
            while len(metadata) > self._max_session_calls:
                victim_id = min(metadata, key=lambda call_id: (metadata[call_id].order, call_id))
                metadata_victim = metadata.pop(victim_id)
                # A metadata-cap eviction also removes any byte-retained table
                # for the same call from the candidate retained set.
                retained.pop(victim_id, None)
                self._delete_call(victim_id, metadata_victim)
            self._connection.execute("COMMIT")
        except BaseException:
            # COMMIT failures may already have closed the transaction. Preserve
            # the original failure for the envelope-only retry.
            with suppress(duckdb.Error):
                self._connection.execute("ROLLBACK")
            raise
        # Only after the commit succeeds: an object the agent can name has to be
        # one that actually exists.
        all_entries = self._retained | {record.call_id: new_entry}
        evicted = set(all_entries) - set(retained)
        for call_id in evicted:
            self._allowed_objects.difference_update(all_entries[call_id].tables)
            self._remember_evicted(all_entries[call_id].tables)
            if call_id in metadata:
                metadata[call_id] = replace(metadata[call_id], tables=())
        self._retained = retained
        self._retained_bytes = self._retained_total(retained)
        removed_metadata = set(self._metadata) - set(metadata)
        for call_id in removed_metadata:
            self._allowed_objects.difference_update(self._metadata[call_id].tables)
            self._remember_evicted(self._metadata[call_id].tables)
        remaining_scopes = {entry.scope_id for entry in metadata.values()}
        for call_id in removed_metadata:
            scope_id = self._metadata[call_id].scope_id
            if scope_id not in remaining_scopes:
                self._allowed_objects.discard(scope_view_name(scope_id))
        self._metadata = metadata
        self._allowed_objects.update(table for entry in retained.values() for table in entry.tables)
        self._allowed_objects.add(view)
        return fits_budget

    @staticmethod
    def _retained_total(retained: dict[str, _RetainedCall]) -> int:
        return sum(entry.size for entry in retained.values())

    def _retention_size(self, record: CallRecord, plans: list[TablePlan]) -> int:
        """Estimate retained state using deterministic serialized representations.

        DuckDB's allocator is deliberately not used as a budget meter: its
        accounting is engine-version and query-plan dependent. Counting the
        envelope's wire payload plus the rows Sluice asks DuckDB to retain gives
        a stable, conservative measure for eviction and includes both channels.
        """
        payload = record.payload
        stored_blocks = (
            None
            if payload.value is None
            and payload.text is None
            and payload.structured is None
            and not payload.blocks
            else payload.blocks
        )
        # Charge each envelope representation independently. In particular,
        # dual-channel calls retain both the parsed value and the raw text.
        json_fields = (
            record.args,
            payload.value,
            stored_blocks,
            payload.structured,
            record.content_kinds,
            record.flat_tables,
            record.source_paths,
        )
        size = sum(self._json_size(value) for value in json_fields if value is not None)
        text_fields = (
            record.call_id,
            record.scope_id,
            record.server,
            record.tool,
            payload.text,
            str(payload.channel),
            record.failure_class,
            record.flat_reason,
        )
        size += sum(len(value.encode("utf-8")) for value in text_fields if value is not None)
        size += sum(
            len(str(value).encode("utf-8"))
            for value in (
                record.seq,
                payload.conflict,
                payload.byte_size,
                payload.wire_bytes,
                record.is_error,
                record.started_at,
                record.ended_at,
                record.duration_ms,
            )
        )
        for plan in plans:
            size += len(plan.name.encode("utf-8")) + len(plan.source_path.encode("utf-8"))
            size += sum(self._json_size(row) for row in plan.records)
        return max(size, 1)

    @staticmethod
    def _json_size(value: object) -> int:
        return len(json.dumps(value, default=str).encode("utf-8"))

    @staticmethod
    def _metadata_payload(payload: SelectedPayload) -> SelectedPayload:
        return replace(
            payload,
            value=None,
            text=None,
            blocks=[],
            structured=None,
        )

    def _evict(self, call_id: str, entry: _RetainedCall, *, reason: str) -> None:
        """Drop a call's tables and clear payload columns, preserving metadata."""
        for table in entry.tables:
            self._connection.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        self._connection.execute(
            f"UPDATE {quote_ident(ENVELOPE_TABLE)} SET args = NULL, result = NULL, "
            "result_text = NULL, result_blocks = NULL, result_structured = NULL, "
            "flat_tables = [], source_paths = [], "
            "flat_reason = CASE WHEN flat_reason IS NULL THEN ? "
            "ELSE ? || '; prior=' || flat_reason END WHERE call_id = ?",
            (reason, reason, call_id),
        )

    def _remember_evicted(self, tables: tuple[str, ...]) -> None:
        for table in tables:
            if table in self._evicted_tables:
                self._evicted_table_order.remove(table)
            self._evicted_tables.add(table)
            self._evicted_table_order.append(table)
            while len(self._evicted_table_order) > self._evicted_table_limit:
                self._evicted_tables.discard(self._evicted_table_order.popleft())

    def _delete_call(self, call_id: str, entry: _CallMetadata) -> None:
        """Delete metadata-cap evictions and their catalog objects atomically."""
        for table in entry.tables:
            self._connection.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        self._connection.execute(
            f"DELETE FROM {quote_ident(ENVELOPE_TABLE)} WHERE call_id = ?", (call_id,)
        )
        remaining = self._connection.execute(
            f"SELECT 1 FROM {quote_ident(ENVELOPE_TABLE)} WHERE scope_id = ? LIMIT 1",
            (entry.scope_id,),
        ).fetchone()
        if remaining is None:
            self._connection.execute(
                f"DROP VIEW IF EXISTS {quote_ident(scope_view_name(entry.scope_id))}"
            )

    def _ensure_scope_view(self, scope_id: str) -> str:
        if not SCOPE_PATTERN.match(scope_id):
            # Scope ids are hex by construction, and this value is interpolated
            # into SQL. Refuse anything that is not, rather than trusting the
            # caller.
            raise ValueError(f"scope id is not 32 hex characters: {scope_id!r}")
        name = scope_view_name(scope_id)
        self._connection.execute(
            f"CREATE OR REPLACE VIEW {quote_ident(name)} AS "
            f"SELECT * FROM {quote_ident(ENVELOPE_TABLE)} WHERE scope_id = '{scope_id}'"
        )
        return name

    def _insert_envelope(self, record: CallRecord) -> None:
        payload = record.payload
        # Passthrough records retain only sizes and block kinds. In particular,
        # result_blocks is a payload column and must remain NULL even when the
        # in-memory selector kept a metadata-only description for the handle.
        blocks = (
            None
            if payload.value is None
            and payload.text is None
            and payload.structured is None
            and not payload.blocks
            else payload.blocks
        )
        row: tuple[Any, ...] = (
            record.call_id,
            record.scope_id,
            record.seq,
            record.server,
            record.tool,
            _json_or_none(record.args),
            _json_or_none(payload.value),
            payload.text,
            _json_or_none(blocks),
            _json_or_none(payload.structured),
            str(payload.channel),
            payload.conflict,
            payload.byte_size,
            payload.wire_bytes,
            record.is_error,
            record.failure_class,
            record.content_kinds,
            record.flat_tables,
            record.flat_reason,
            record.source_paths,
            record.started_at,
            record.ended_at,
            record.duration_ms,
        )
        self._connection.execute(_INSERT, row)

    def _create_flat(self, plan: TablePlan, call_id: str) -> None:
        """Explicit DDL plus executemany, never `read_json`: the lockdown is
        database-global and blocks DuckDB's own file readers (spec 5.4). Every
        identifier is quoted, including column names taken verbatim from
        downstream payload keys."""
        columns: list[ColumnSpec] = plan.columns
        declared = ", ".join(f"{quote_ident(c.name)} {c.type}" for c in columns)
        trailing = f"{quote_ident(ROW_COLUMN)} BIGINT, {quote_ident(CALL_COLUMN)} VARCHAR"
        separator = ", " if declared else ""
        self._connection.execute(
            f"CREATE TABLE {quote_ident(plan.name)} ({declared}{separator}{trailing})"
        )
        placeholders = ", ".join(["?"] * (len(columns) + 2))
        rows = [
            (
                *(coerce(record.get(column.name), column.type) for column in columns),
                record[ROW_COLUMN],
                call_id,
            )
            for record in plan.records
        ]
        if rows:
            self._connection.executemany(
                f"INSERT INTO {quote_ident(plan.name)} VALUES ({placeholders})", rows
            )
