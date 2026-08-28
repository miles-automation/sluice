"""The DuckDB scratch database: lockdown, envelope, sequence numbers."""

import json
import logging
from types import TracebackType
from typing import Any, Self

import anyio
import duckdb

from sluice.config import Limits
from sluice.models import CallRecord

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


class Store:
    """Owns the connection Sluice writes through.

    The lockdown is database-global, so it applies to Sluice's own SQL exactly as
    it applies to the agent's. That is why materialization cannot use `read_json`
    (spec 5.4). Do not relax it to fix a write.
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection
        self._lock = anyio.Lock()
        self._sequences: dict[str, int] = {}

    @classmethod
    def open(cls, limits: Limits) -> Self:
        connection = duckdb.connect(":memory:")
        for statement in LOCKDOWN_TEMPLATE:
            connection.execute(statement.format(max_memory=limits.duckdb_max_memory))
        # CREATE TABLE still works after the lockdown; only external access and
        # further configuration changes are closed off.
        connection.execute(ENVELOPE_DDL)
        return cls(connection)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._connection

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

    async def next_seq(self, mounted: str) -> int:
        async with self._lock:
            nxt = self._sequences.get(mounted, 0) + 1
            self._sequences[mounted] = nxt
            return nxt

    async def record(self, record: CallRecord) -> None:
        """Write one envelope row.

        FR-8 is conditional on this succeeding. There is no fallback journal: if
        the envelope write fails, the caller logs and returns the downstream
        result unmodified rather than failing a call that worked.
        """
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
        async with self._lock:
            await anyio.to_thread.run_sync(self._insert, row)

    def _insert(self, row: tuple[Any, ...]) -> None:
        self._connection.execute(_INSERT, row)
