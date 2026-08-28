"""The scratch database: lockdown, envelope, atomicity."""

from datetime import datetime
from typing import Any

import duckdb
import pytest

from sluice.infer import ColumnSpec, ColumnType
from sluice.models import CallRecord, PayloadChannel, SelectedPayload, TablePlan
from sluice.store import ENVELOPE_TABLE, Store

pytestmark = pytest.mark.anyio

STARTED = datetime(2026, 8, 28, 12, 0, 0)
ENDED = datetime(2026, 8, 28, 12, 0, 1)


def _payload() -> SelectedPayload:
    return SelectedPayload(
        channel=PayloadChannel.TEXT,
        value={"items": [1, 2]},
        text='{"items": [1, 2]}',
        blocks=[{"type": "text", "text": '{"items": [1, 2]}'}],
        structured={"items": [9]},
        byte_size=17,
        wire_bytes=99,
        conflict=True,
    )


def _record(call_id: str = "c1", **overrides: Any) -> CallRecord:
    base: dict[str, Any] = {
        "call_id": call_id,
        "scope_id": "a" * 32,
        "seq": 1,
        "server": "fake",
        "tool": "rows",
        "args": {"n": 5},
        "payload": _payload(),
        "is_error": False,
        "failure_class": None,
        "content_kinds": ["text"],
        "started_at": STARTED,
        "ended_at": ENDED,
    }
    base.update(overrides)
    return CallRecord(**base)


def _plan(name: str = "t1", records: list[dict[str, Any]] | None = None) -> TablePlan:
    return TablePlan(
        name=name,
        source_path="$.items",
        columns=[ColumnSpec(name="a", type=ColumnType.BIGINT, exact=True)],
        records=records if records is not None else [{"a": 1, "_row": 0}, {"a": 2, "_row": 1}],
    )


def _user_tables(store: Store) -> list[str]:
    rows = store.connection.execute(
        "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows if r[0] != ENVELOPE_TABLE]


# --------------------------------------------------------------------------
# Lockdown
# --------------------------------------------------------------------------


def test_lockdown_closes_external_access(store: Store) -> None:
    with pytest.raises(duckdb.Error):
        store.connection.execute("SELECT * FROM read_csv('/etc/hosts')").fetchall()


def test_lockdown_freezes_configuration(store: Store) -> None:
    """`threads` is settable on a fresh connection, so this proves the lock and
    not merely that the option is unsettable."""
    with pytest.raises(duckdb.Error):
        store.connection.execute("SET threads = 3")


def test_sluice_can_still_create_tables_after_the_lockdown(store: Store) -> None:
    store.connection.execute("CREATE TABLE t (a BIGINT)")
    store.connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    assert store.connection.execute("SELECT sum(a) FROM t").fetchall()[0][0] == 3


# --------------------------------------------------------------------------
# The envelope, in full
# --------------------------------------------------------------------------


async def test_every_envelope_column_round_trips(store: Store) -> None:
    """Asserted in full rather than field by field.

    A partial assertion let mutations that stored the wrong `_call_id`, dropped
    `result_structured`, or discarded result metadata pass the entire suite.
    """
    await store.commit_call(_record(), [])
    row = store.connection.execute(
        f"SELECT call_id, scope_id, seq, server, tool, args, result, result_text, "
        f"result_blocks, result_structured, source_channel, channel_conflict, byte_size, "
        f"wire_bytes, is_error, failure_class, content_kinds, flat_tables, flat_reason, "
        f"source_paths, started_at, ended_at, duration_ms FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert row[0] == "c1"
    assert row[1] == "a" * 32
    assert row[2] == 1
    assert row[3] == "fake"
    assert row[4] == "rows"
    assert row[5] == '{"n": 5}'
    assert row[6] == '{"items": [1, 2]}'
    assert row[7] == '{"items": [1, 2]}'
    assert row[8] == '[{"type": "text", "text": "{\\"items\\": [1, 2]}"}]'
    assert row[9] == '{"items": [9]}'
    assert row[10] == "text"
    assert row[11] is True
    assert row[12] == 17
    assert row[13] == 99
    assert row[14] is False
    assert row[15] is None
    assert row[16] == ["text"]
    assert row[17] == []
    assert row[18] is None
    assert row[19] == []
    assert row[20] == STARTED
    assert row[21] == ENDED
    assert row[22] == 1000


async def test_flat_table_lineage_is_stored(store: Store) -> None:
    """`_row` and `_call_id` are the join back to the envelope. Storing them
    wrong is invisible unless something asserts their values."""
    record = _record(flat_tables=["t1"], source_paths=["$.items"])
    await store.commit_call(record, [_plan()])
    rows = store.connection.execute('SELECT a, _row, _call_id FROM "t1" ORDER BY _row').fetchall()
    assert rows == [(1, 0, "c1"), (2, 1, "c1")]


async def test_row_ordinals_follow_source_order(store: Store) -> None:
    records = [{"a": 10 - i, "_row": i} for i in range(5)]
    await store.commit_call(_record(), [_plan(records=records)])
    stored = store.connection.execute('SELECT a FROM "t1" ORDER BY _row').fetchall()
    assert [r[0] for r in stored] == [10, 9, 8, 7, 6]


# --------------------------------------------------------------------------
# Atomicity
# --------------------------------------------------------------------------


async def test_a_failing_table_leaves_nothing_behind(store: Store) -> None:
    """Coercion of a dict into BIGINT fails during insert, after CREATE TABLE."""
    bad = _plan(records=[{"a": {"not": "an int"}, "_row": 0}])
    with pytest.raises(TypeError):
        await store.commit_call(_record(), [bad])
    assert _user_tables(store) == []
    assert store.connection.execute(f"SELECT count(*) FROM {ENVELOPE_TABLE}").fetchall()[0][0] == 0


async def test_a_failing_second_table_rolls_back_the_first(store: Store) -> None:
    """Several candidate arrays are written in one call (spec 5.2), so a partial
    success would leave the agent a handle naming tables that do not all exist."""
    good = _plan("first")
    bad = _plan("second", records=[{"a": {"nope": 1}, "_row": 0}])
    with pytest.raises(TypeError):
        await store.commit_call(_record(), [good, bad])
    assert _user_tables(store) == []


async def test_a_failing_envelope_write_rolls_back_the_tables(store: Store) -> None:
    """The envelope is the only record that a table exists. Tables written
    without it are unreachable and invisible."""
    await store.commit_call(_record("dup"), [])
    with pytest.raises(duckdb.ConstraintException):
        # Same primary key, so the envelope insert fails after the tables are made.
        await store.commit_call(_record("dup", flat_tables=["orphan"]), [_plan("orphan")])
    assert _user_tables(store) == []


async def test_a_successful_commit_leaves_both(store: Store) -> None:
    record = _record(flat_tables=["t1"], source_paths=["$.items"])
    await store.commit_call(record, [_plan()])
    assert _user_tables(store) == ["t1"]
    stored = store.connection.execute(
        f"SELECT flat_tables, source_paths FROM {ENVELOPE_TABLE}"
    ).fetchall()[0]
    assert stored == (["t1"], ["$.items"])


async def test_the_connection_stays_usable_after_a_rollback(store: Store) -> None:
    with pytest.raises(TypeError):
        await store.commit_call(_record(), [_plan(records=[{"a": {"x": 1}, "_row": 0}])])
    await store.commit_call(_record("after"), [_plan("later")])
    assert _user_tables(store) == ["later"]


# --------------------------------------------------------------------------
# Sequences
# --------------------------------------------------------------------------


async def test_sequences_are_monotonic_per_tool_and_independent(store: Store) -> None:
    assert await store.next_call_seq("a") == 1
    assert await store.next_call_seq("a") == 2
    assert await store.next_call_seq("b") == 1
    # One call may create several tables, so the counters must not share.
    assert await store.next_table_seq("a") == 1
    assert await store.next_table_seq("a") == 2
    assert await store.next_call_seq("a") == 3
