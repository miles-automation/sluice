"""The scratch database: lockdown, envelope, sequence numbers."""

from datetime import datetime

import duckdb
import pytest

from sluice.models import CallRecord, PayloadChannel, SelectedPayload
from sluice.store import ENVELOPE_TABLE, Store

pytestmark = pytest.mark.anyio


def _record(call_id: str = "c1", tool: str = "rows", seq: int = 1) -> CallRecord:
    started = datetime(2026, 8, 28, 12, 0, 0)
    ended = datetime(2026, 8, 28, 12, 0, 1)
    return CallRecord(
        call_id=call_id,
        scope_id="abcd1234",
        seq=seq,
        server="fake",
        tool=tool,
        args={"n": 5},
        payload=SelectedPayload(
            channel=PayloadChannel.TEXT,
            value={"items": [1, 2]},
            text='{"items": [1, 2]}',
            blocks=[{"type": "text", "text": '{"items": [1, 2]}'}],
            byte_size=17,
            wire_bytes=99,
        ),
        is_error=False,
        failure_class=None,
        content_kinds=["text"],
        started_at=started,
        ended_at=ended,
    )


def test_lockdown_closes_external_access(store: Store) -> None:
    with pytest.raises(duckdb.Error):
        store.connection.execute("SELECT * FROM read_csv('/etc/hosts')").fetchall()
    with pytest.raises(duckdb.Error):
        store.connection.execute("SET enable_external_access = true")


def test_sluice_can_still_create_tables_after_the_lockdown(store: Store) -> None:
    """What makes file-free materialization possible at all (spec 5.4)."""
    store.connection.execute("CREATE TABLE t (a BIGINT)")
    store.connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    assert store.connection.execute("SELECT sum(a) FROM t").fetchall()[0][0] == 3


async def test_record_writes_one_row(store: Store) -> None:
    await store.record(_record())
    rows = store.connection.execute(
        f"SELECT call_id, scope_id, server, tool, source_channel, byte_size, duration_ms "
        f"FROM {ENVELOPE_TABLE}"
    ).fetchall()
    assert rows == [("c1", "abcd1234", "fake", "rows", "text", 17, 1000)]


async def test_json_columns_round_trip(store: Store) -> None:
    await store.record(_record())
    result, args = store.connection.execute(
        f"SELECT result, args FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert '"items"' in result
    assert '"n"' in args


async def test_sequence_is_monotonic_per_tool(store: Store) -> None:
    assert await store.next_seq("a") == 1
    assert await store.next_seq("a") == 2
    assert await store.next_seq("b") == 1
    assert await store.next_seq("a") == 3
