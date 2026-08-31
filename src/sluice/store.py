"""The DuckDB scratch database: lockdown, envelope, sequence numbers."""

import json
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import anyio
import duckdb

from sluice.config import SESSION_RETENTION_DEFAULT, Limits
from sluice.gate import SCOPE_PATTERN
from sluice.infer import ColumnSpec, coerce
from sluice.models import CallRecord, TablePlan
from sluice.naming import quote_ident
from sluice.shape import CALL_COLUMN, ROW_COLUMN

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._connection = connection
        self._max_session_bytes = max_session_bytes
        self._lock = anyio.Lock()
        self._call_sequences: dict[str, int] = {}
        self._table_sequences: dict[str, int] = {}
        self._retention_sequence = 0
        self._retained: dict[str, _RetainedCall] = {}
        self._retained_bytes = 0
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
        return cls(connection, limits.max_session_bytes)

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
        async with self._lock:
            return await anyio.to_thread.run_sync(self._commit, record, plans)

    def _commit(self, record: CallRecord, plans: list[TablePlan]) -> bool:
        size = self._retention_size(record, plans)
        order = record.retention_seq or self._retention_sequence + 1
        new_entry = _RetainedCall(
            order=order,
            size=size,
            scope_id=record.scope_id,
            tables=tuple(plan.name for plan in plans),
        )
        retained = dict(self._retained)
        retained[record.call_id] = new_entry
        self._connection.execute("BEGIN TRANSACTION")
        try:
            for plan in plans:
                self._create_flat(plan, record.call_id)
            self._insert_envelope(record)
            view = self._ensure_scope_view(record.scope_id)
            while self._retained_total(retained) > self._max_session_bytes:
                victim_id = min(
                    retained,
                    key=lambda call_id: (retained[call_id].order, call_id),
                )
                victim = retained.pop(victim_id)
                self._evict(
                    victim_id,
                    victim,
                    reason=(
                        "retention_budget_exceeded"
                        if victim_id == record.call_id
                        else "retention_evicted"
                    ),
                )
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        # Only after the commit succeeds: an object the agent can name has to be
        # one that actually exists.
        all_entries = self._retained | {record.call_id: new_entry}
        evicted = set(all_entries) - set(retained)
        for call_id in evicted:
            self._allowed_objects.difference_update(all_entries[call_id].tables)
        self._retained = retained
        self._retained_bytes = self._retained_total(retained)
        self._allowed_objects.update(table for entry in retained.values() for table in entry.tables)
        self._allowed_objects.add(view)
        return record.call_id in retained

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
        size = max(record.payload.wire_bytes, record.payload.byte_size, 1)
        if record.args is not None:
            size += len(json.dumps(record.args, default=str).encode("utf-8"))
        for plan in plans:
            size += len(plan.name) + len(plan.source_path)
            size += sum(len(json.dumps(row, default=str).encode("utf-8")) for row in plan.records)
        return size

    def _evict(self, call_id: str, entry: _RetainedCall, *, reason: str) -> None:
        """Drop a call's tables and clear payload columns, preserving metadata."""
        for table in entry.tables:
            self._connection.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        self._connection.execute(
            f"UPDATE {quote_ident(ENVELOPE_TABLE)} SET args = NULL, result = NULL, "
            "result_text = NULL, result_blocks = NULL, result_structured = NULL, "
            "flat_tables = [], source_paths = [], flat_reason = ? WHERE call_id = ?",
            (reason, call_id),
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
        row: tuple[Any, ...] = (
            record.call_id,
            record.scope_id,
            record.seq,
            record.server,
            record.tool,
            _json_or_none(record.args),
            _json_or_none(payload.value),
            payload.text,
            _json_or_none(payload.blocks),
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
