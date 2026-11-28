import gc
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types

from sluice import naming
from sluice._store_worker import reply_frame
from sluice.config import Limits
from sluice.gate import QueryRejectedError
from sluice.intercept import Interceptor
from sluice.query import QueryTool
from sluice.store import Store
from sluice.supervisor import (
    StoreProcess,
    SupervisedStore,
    describe_bytes,
    memory_sampler,
    open_store,
)

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

CAP = 256 * 1024 * 1024
LIMITS = Limits(store_max_memory=CAP, duckdb_max_memory="64MB")
MOUNTED = naming.mounted_name("srv", "rows")


def _hostile(table: str) -> str:
    inner = "s"
    for _ in range(13):
        inner = f"replace({inner}, 'a', 'aaaa')"
    return f'SELECT sum(length({inner})) FROM "{table}"'


async def _materialize(
    interceptor: Interceptor, rows: list[dict[str, Any]], meta: dict[str, str] | None = None
) -> dict[str, Any]:
    result = await interceptor.intercept(
        server="srv",
        tool="rows",
        mounted=MOUNTED,
        arguments=None,
        result=types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"items": rows}))]
        ),
        meta=meta,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    assert result.is_error is not True
    structured: dict[str, Any] | None = result.structured_content
    assert structured is not None
    return structured


WATCHER_KILL_BYTES = 2 * 1024**3


@contextmanager
def _watched(limits: Limits) -> Iterator[SupervisedStore]:
    sampler = memory_sampler()
    stop = threading.Event()
    killed: list[int] = []

    def watch() -> None:
        while not stop.wait(0.02):
            process = store.process
            used = sampler(process.pid) if sampler is not None and process is not None else None
            if process is not None and used is not None and used > WATCHER_KILL_BYTES:
                os.kill(process.pid, signal.SIGKILL)
                killed.append(used)

    with SupervisedStore.open(limits) as store:
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield store
            time.sleep(0.3)
        finally:
            stop.set()
            watcher.join()
    assert not killed, f"the outside watcher killed the store at {describe_bytes(killed[0])}"


def _seed_rows() -> list[dict[str, Any]]:
    return [{"id": index, "s": "a"} for index in range(1, 21)]


async def test_hostile_query_resets_the_store_and_the_server_keeps_serving() -> None:
    with _watched(LIMITS) as store:
        interceptor = Interceptor(store, LIMITS, query_available=True)
        query = QueryTool(store, LIMITS)
        handle = await _materialize(interceptor, _seed_rows())
        table = handle["tables"][0]["name"]
        process = store.process
        assert process is not None
        with pytest.raises(QueryRejectedError) as caught:
            await query.run(_hostile(table))
        message = str(caught.value)
        assert message.startswith("store reset:")
        assert "store_max_memory" in message and str(CAP) in message
        assert store.resets == 1
        overshoot = process.peak_bytes - CAP
        assert 0 < overshoot < 512 * 1024 * 1024, describe_bytes(process.peak_bytes)
        assert "1 row(s) shown." in await query.run("SELECT 42 AS answer")
        fresh = await _materialize(interceptor, [{"id": 7}])
        assert "| 7 |" in await query.run(f'SELECT id FROM "{fresh["tables"][0]["name"]}"')


async def test_every_pre_reset_handle_fails_loudly() -> None:
    meta = {"conversationId": "reset-conversation"}
    with _watched(LIMITS) as store:
        interceptor = Interceptor(store, LIMITS, query_available=True)
        query = QueryTool(store, LIMITS)
        before = await _materialize(interceptor, _seed_rows(), meta)
        table = before["tables"][0]["name"]
        envelope = before["envelope_table"]
        with pytest.raises(QueryRejectedError, match="store reset"):
            await query.run(_hostile(table))
        after = await _materialize(interceptor, [{"id": 1}], meta)
        assert after["scope_id"] == before["scope_id"]
        assert after["envelope_table"] != envelope
        assert after["tables"][0]["name"] != table
        for sql in (
            f'SELECT count(*) FROM "{table}"',
            f'SELECT count(*) FROM "{envelope}"',
            f"SELECT count(*) FROM {envelope.upper()}",
        ):
            with pytest.raises(QueryRejectedError, match="lost when the store was reset"):
                await query.run(sql)
        text = await query.run(f'SELECT count(*) FROM "{after["envelope_table"]}"')
        assert "| 1 |" in text


async def test_materialization_racing_a_reset_keeps_the_tool_call_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = StoreProcess.start
    killed = False

    def kill_during_commit(self: StoreProcess, message: dict[str, Any]) -> Any:
        nonlocal killed
        if message.get("op") == "commit" and not killed:
            killed = True
            os.kill(self.pid, signal.SIGKILL)
        return original(self, message)

    monkeypatch.setattr(StoreProcess, "start", kill_during_commit)
    with SupervisedStore.open(LIMITS) as store:
        interceptor = Interceptor(store, LIMITS, query_available=True)
        query = QueryTool(store, LIMITS)
        handle = await _materialize(interceptor, _seed_rows())
        assert killed
        assert store.resets == 1
        assert handle["tables"] == []
        assert str(handle["flat_reason"]).startswith("load_failed: StoreResetError: store reset")
        text = await query.run(
            f'SELECT count(*) FROM "{handle["envelope_table"]}" '
            f"WHERE call_id = '{handle['call_id']}'"
        )
        assert "| 1 |" in text


async def test_materialization_concurrent_with_a_hostile_query_never_fails() -> None:
    with _watched(LIMITS) as store:
        interceptor = Interceptor(store, LIMITS, query_available=True)
        query = QueryTool(store, LIMITS)
        seed = await _materialize(interceptor, _seed_rows())
        handles: list[dict[str, Any]] = []
        failures: list[str] = []

        async def hostile() -> None:
            try:
                await query.run(_hostile(seed["tables"][0]["name"]))
            except QueryRejectedError as exc:
                failures.append(str(exc))

        async def calls() -> None:
            for index in range(20):
                rows = [{"id": index, "pad": "x" * 2000} for _ in range(200)]
                handles.append(await _materialize(interceptor, rows))

        async with anyio.create_task_group() as group:
            group.start_soon(hostile)
            group.start_soon(calls)
        assert failures and failures[0].startswith("store reset:")
        assert len(handles) == 20
        for handle in handles:
            reason = handle["flat_reason"]
            assert handle["tables"] or str(reason).startswith("load_failed")


async def test_query_timeout_interrupts_inside_the_store_without_a_reset() -> None:
    limits = Limits(store_max_memory=CAP, duckdb_max_memory="64MB", query_timeout_seconds=0.5)
    with SupervisedStore.open(limits) as store:
        interceptor = Interceptor(store, limits, query_available=True)
        query = QueryTool(store, limits)
        handle = await _materialize(interceptor, [{"a": index} for index in range(1000)])
        table = handle["tables"][0]["name"]
        started = time.monotonic()
        with pytest.raises(QueryRejectedError, match="timeout"):
            await query.run(
                f'SELECT count(*) FROM "{table}" x, "{table}" y, "{table}" z '
                "WHERE x.a + y.a + z.a > 0"
            )
        assert time.monotonic() - started < 3
        assert store.resets == 0
        assert "| 1000 |" in await query.run(f'SELECT count(*) FROM "{table}"')


async def test_timed_out_hostile_query_stays_watched_until_duckdb_stops() -> None:
    limits = Limits(store_max_memory=CAP, duckdb_max_memory="64MB", query_timeout_seconds=0.05)
    with _watched(limits) as store:
        interceptor = Interceptor(store, limits, query_available=True)
        query = QueryTool(store, limits)
        handle = await _materialize(interceptor, _seed_rows())
        with pytest.raises(QueryRejectedError) as caught:
            await query.run(_hostile(handle["tables"][0]["name"]))
        message = str(caught.value)
        assert message.startswith(("store reset:", "query exceeded the 0.05s timeout"))
        if message.startswith("store reset:"):
            assert store.resets == 1
            return
        sampler = memory_sampler()
        process = store.process
        assert sampler is not None and process is not None
        peak = 0
        for _ in range(40):
            peak = max(peak, sampler(process.pid) or 0)
            time.sleep(0.025)
        assert peak < CAP, f"the store kept growing after the timeout: {peak} bytes"
        assert store.resets == 0


async def test_query_replies_stay_within_the_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = StoreProcess.wait
    sizes: list[int] = []

    def measured(self: StoreProcess, request: int, pending: Any) -> dict[str, Any]:
        reply = original(self, request, pending)
        sizes.append(len(json.dumps(reply)))
        return reply

    monkeypatch.setattr(StoreProcess, "wait", measured)
    limits = Limits(store_max_memory=2 * CAP, duckdb_max_memory="64MB")
    with SupervisedStore.open(limits) as store:
        interceptor = Interceptor(store, limits, query_available=True)
        query = QueryTool(store, limits)
        handle = await _materialize(
            interceptor, [{"id": index, "s": "\U0001f600" * 200} for index in range(150)]
        )
        table = handle["tables"][0]["name"]
        columns = ", ".join(f's AS "c{index}"' for index in range(300))
        text = await query.run(f'SELECT {columns} FROM "{table}"')
        assert max(sizes) < 4 * limits.query_max_bytes, sizes
        assert len(text.encode("utf-8")) <= limits.query_max_bytes
        notices = text.rsplit("\n", 1)[-1]
        shown = int(notices.split(" row(s) shown.")[0])
        omitted = int(notices.split(" row(s) omitted")[0].rsplit(" ", 1)[-1])
        assert shown + omitted == limits.query_max_rows
        assert "Additional rows exist beyond max_rows" in notices
        narrow = await query.run(f'SELECT "id" FROM "{table}" ORDER BY "id"', max_rows=3)
        assert narrow.endswith(
            "3 row(s) shown. Additional rows exist beyond max_rows; the exact number is not known."
        )


def test_an_oversized_reply_becomes_a_query_error() -> None:
    frame = reply_frame({"id": 7, "ok": True, "rows": [["x" * 100]]}, max_bytes=64)
    reply = json.loads(frame[4:])
    assert reply["id"] == 7 and reply["ok"] is False and reply["kind"] == "rejected"
    assert "over the 64 byte limit" in reply["message"]


async def test_gate_rejections_and_engine_errors_cross_the_process_boundary() -> None:
    with SupervisedStore.open(LIMITS) as store:
        query = QueryTool(store, LIMITS)
        with pytest.raises(QueryRejectedError, match="unknown table"):
            await query.run("SELECT * FROM sluice_calls")
        with pytest.raises(QueryRejectedError, match="only SELECT"):
            await query.run("CREATE TABLE x (a INT)")
        with pytest.raises(QueryRejectedError, match="CatalogException") as caught:
            await query.run("SELECT lenght('x')")
        assert "Did you mean" not in str(caught.value)
        assert store.resets == 0


async def test_store_process_never_writes_to_stdout(tmp_path: Path) -> None:
    (tmp_path / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import sys
            if "sluice._store_worker" in sys.orig_argv:
                print("CHILD-STDOUT-MARKER", flush=True)
            """
        ),
        encoding="utf-8",
    )
    script = textwrap.dedent(
        """
        import anyio
        from sluice.config import Limits
        from sluice.query import QueryTool
        from sluice.supervisor import SupervisedStore

        async def main() -> None:
            limits = Limits(store_max_memory=268435456, duckdb_max_memory="64MB")
            with SupervisedStore.open(limits) as store:
                await QueryTool(store, limits).run("SELECT 1")
                process = store.process
                assert process is not None
                process.kill("test")

        anyio.run(main)
        """
    )
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert "CHILD-STDOUT-MARKER" in completed.stderr


async def test_store_process_exits_when_its_parent_dies() -> None:
    script = textwrap.dedent(
        """
        import sys, time
        from sluice.config import Limits
        from sluice.supervisor import SupervisedStore

        store = SupervisedStore.open(Limits(store_max_memory=268435456, duckdb_max_memory="64MB"))
        process = store.process
        assert process is not None
        print(process.pid, flush=True)
        time.sleep(60)
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert parent.stdout is not None
    child = int(parent.stdout.readline())
    parent.kill()
    parent.wait()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(child, signal.SIGKILL)
    pytest.fail("the store process outlived its parent")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA is Linux only")
async def test_store_process_runs_under_rlimit_data_on_linux() -> None:
    with SupervisedStore.open(LIMITS) as store:
        process = store.process
        assert process is not None and process.data_limit is not None
        status = Path(f"/proc/{process.pid}/status").read_text(encoding="ascii")
        vm_data = next(
            int(row.split()[1]) * 1024 for row in status.splitlines() if row.startswith("VmData:")
        )
        assert CAP < process.data_limit - vm_data <= CAP + 64 * 1024 * 1024
        limits = Path(f"/proc/{process.pid}/limits").read_text(encoding="ascii")
        line = next(row for row in limits.splitlines() if row.startswith("Max data size"))
        assert str(process.data_limit) in line
        assert "| 1 |" in await QueryTool(store, LIMITS).run("SELECT 1 AS one")


def test_memory_sampler_reads_this_platform() -> None:
    sampler = memory_sampler()
    if sampler is None:
        pytest.skip("no RSS sampler on this platform")
    used = sampler(os.getpid())
    assert used is not None and used > 1024 * 1024


@pytest.mark.parametrize("platform", ["win32", "cygwin", "freebsd14"])
def test_unsupported_platforms_fall_back_loudly(
    platform: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level("WARNING", logger="sluice.supervisor"),
        open_store(LIMITS, platform) as store,
    ):
        assert isinstance(store, Store)
    assert f"store_max_memory is not enforced on {platform}" in caplog.text


def test_supported_platforms_use_the_store_process() -> None:
    with open_store(LIMITS, sys.platform) as store:
        assert isinstance(store, SupervisedStore) == (
            sys.platform == "darwin" or sys.platform.startswith("linux")
        )


async def test_resets_do_not_leak_descriptors() -> None:
    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    gc.disable()
    try:
        with SupervisedStore.open(LIMITS) as store:
            query = QueryTool(store, LIMITS)
            await query.run("SELECT 1")
            before = open_descriptors()
            for _ in range(5):
                process = store.process
                assert process is not None
                os.kill(process.pid, signal.SIGKILL)
                deadline = time.monotonic() + 10
                while process.alive and time.monotonic() < deadline:
                    await anyio.sleep(0.01)
                await query.run("SELECT 1")
            assert store.resets == 5
            assert open_descriptors() <= before
    finally:
        gc.enable()


async def test_a_reset_racing_the_stale_check_still_reports_the_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SupervisedStore.open(LIMITS) as store:
        interceptor = Interceptor(store, LIMITS, query_available=True)
        query = QueryTool(store, LIMITS)
        handle = await _materialize(interceptor, _seed_rows())
        original = store._ensure
        raced = False

        def reset_first() -> StoreProcess:
            nonlocal raced
            process = original()
            if not raced:
                raced = True
                process.kill("test reset")
                deadline = time.monotonic() + 10
                while store.resets == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                return original()
            return process

        monkeypatch.setattr(store, "_ensure", reset_first)
        with pytest.raises(QueryRejectedError, match="lost when the store was reset"):
            await query.run(f'SELECT count(*) FROM "{handle["tables"][0]["name"]}"')
        assert raced


async def test_a_table_evicted_before_a_reset_is_still_reported_as_evicted() -> None:
    limits = Limits(
        store_max_memory=CAP,
        duckdb_max_memory="64MB",
        max_payload_bytes=3000,
        max_session_bytes=3500,
    )
    with SupervisedStore.open(limits) as store:
        interceptor = Interceptor(store, limits, query_available=True)
        query = QueryTool(store, limits)
        first = await _materialize(interceptor, [{"id": 1, "padding": "x" * 400}])
        await _materialize(interceptor, [{"id": 2, "padding": "x" * 400}])
        table = first["tables"][0]["name"]
        with pytest.raises(QueryRejectedError, match="was evicted"):
            await query.run(f'SELECT * FROM "{table}"')
        process = store.process
        assert process is not None
        process.kill("test reset")
        assert "1 row(s) shown." in await query.run("SELECT 1")
        assert store.resets == 1
        with pytest.raises(QueryRejectedError, match="was evicted"):
            await query.run(f'SELECT * FROM "{table}"')
        with pytest.raises(QueryRejectedError, match="lost when the store was reset"):
            await query.run(f'SELECT * FROM "{first["envelope_table"]}"')


async def test_in_process_query_gives_up_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def ignores_interrupt(*_args: object) -> object:
        release.wait()
        raise AssertionError("released")

    monkeypatch.setattr("sluice.store.execute_on_cursor", ignores_interrupt)
    monkeypatch.setattr("sluice.store.INTERRUPT_GRACE_SECONDS", 0.2)
    limits = Limits(query_timeout_seconds=0.2)
    safety = threading.Timer(4, release.set)
    safety.start()
    try:
        with Store.open(limits) as store:
            started = time.monotonic()
            with pytest.raises(QueryRejectedError, match="timeout"):
                await QueryTool(store, limits).run("SELECT 1")
            assert time.monotonic() - started < 2
    finally:
        release.set()
        safety.cancel()
