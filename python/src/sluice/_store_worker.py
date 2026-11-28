import os
import resource
import socket
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import duckdb

from sluice.config import Limits
from sluice.execute import (
    AllocationFailedError,
    QueryInterruptedError,
    execute_on_cursor,
    is_allocation_failure,
)
from sluice.gate import QueryRejectedError
from sluice.ipc import (
    BREACH_EXIT,
    REPLY_MAX_BYTES,
    ChannelClosedError,
    Message,
    encode,
    plan_from_wire,
    read_message,
    record_from_wire,
)
from sluice.store import Store


def reply_frame(message: Message, max_bytes: int = REPLY_MAX_BYTES) -> bytes:
    frame = encode(message)
    if len(frame) <= max_bytes:
        return frame
    return encode(
        {
            "id": message.get("id"),
            "ok": False,
            "kind": "rejected",
            "error": "ReplyTooLarge",
            "message": (
                f"the result is {len(frame)} bytes, over the {max_bytes} byte limit for one "
                "reply from the store; select fewer or narrower columns"
            ),
        }
    )


class _Query:
    def __init__(self) -> None:
        self.expired = threading.Event()
        self.cursor: duckdb.DuckDBPyConnection | None = None


class Worker:
    def __init__(self, channel: socket.socket) -> None:
        self._channel = channel
        self._send_lock = threading.Lock()
        self._store: Store | None = None
        self._queries: dict[int, _Query] = {}
        self._queries_lock = threading.Lock()

    def send(self, message: Message) -> None:
        frame = reply_frame(message)
        with self._send_lock:
            self._channel.sendall(frame)

    def breach(self, exc: BaseException) -> None:
        detail = f"an allocation failed inside the store ({type(exc).__name__}: {exc})"
        try:
            self.send({"breach": detail})
        finally:
            os._exit(BREACH_EXIT)

    def serve(self) -> None:
        while True:
            try:
                message = read_message(self._channel)
            except ChannelClosedError, OSError:
                os._exit(0)
            operation = message.get("op")
            if operation == "open":
                self._open(message)
            elif operation == "interrupt":
                self._interrupt(int(message["target"]))
            elif operation == "commit":
                threading.Thread(target=self._commit, args=(message,), daemon=True).start()
            elif operation == "query":
                query = _Query()
                with self._queries_lock:
                    self._queries[int(message["id"])] = query
                threading.Thread(target=self._query, args=(message, query), daemon=True).start()
            else:
                self.send({"id": message.get("id"), "ok": False, "error": "ProtocolError"})

    def _open(self, message: Message) -> None:
        try:
            limits = Limits(**message["limits"])
            self._store = Store.open(limits, int(message["generation"]))
        except Exception as exc:
            self.send({"id": 0, "ok": False, "error": type(exc).__name__, "message": str(exc)})
            return
        self.send({"id": 0, "ok": True, "data_limit": limit_data(limits.store_max_memory)})

    def _require(self) -> Store:
        if self._store is None:
            raise RuntimeError("the store was not opened")
        return self._store

    def _interrupt(self, target: int) -> None:
        with self._queries_lock:
            query = self._queries.get(target)
        if query is None:
            return
        query.expired.set()
        cursor = query.cursor
        if cursor is not None:
            with suppress(duckdb.Error):
                cursor.interrupt()

    def _commit(self, message: Message) -> None:
        request = message["id"]
        try:
            record = record_from_wire(message["record"])
            plans = [plan_from_wire(plan) for plan in message["plans"]]
            outcome = self._require()._commit(record, plans)
        except Exception as exc:
            if is_allocation_failure(exc):
                self.breach(exc)
            self.send(
                {"id": request, "ok": False, "error": type(exc).__name__, "message": str(exc)}
            )
            return
        self.send(
            {
                "id": request,
                "ok": True,
                "retained": outcome.retained,
                "envelope": outcome.envelope,
                "evicted": list(outcome.evicted),
            }
        )

    def _query(self, message: Message, query: _Query) -> None:
        request = int(message["id"])
        reply: dict[str, Any]
        try:
            store = self._require()
            cursor = store.connection.cursor()
            query.cursor = cursor
            try:
                rows = execute_on_cursor(
                    cursor,
                    message["sql"],
                    int(message["limit"]),
                    store.allowed_objects,
                    store.evicted_tables,
                    int(message["cell_bytes"]),
                    int(message["output_bytes"]),
                    query.expired,
                )
            finally:
                query.cursor = None
                cursor.close()
            reply = {
                "id": request,
                "ok": True,
                "columns": rows.columns,
                "rows": rows.rows,
                "more_rows": rows.more_rows,
                "omitted": rows.omitted,
            }
        except AllocationFailedError as exc:
            self.breach(exc)
            return
        except QueryInterruptedError:
            reply = {"id": request, "ok": False, "kind": "interrupted"}
        except QueryRejectedError as exc:
            reply = {"id": request, "ok": False, "kind": "rejected", "message": str(exc)}
        except Exception as exc:
            if is_allocation_failure(exc):
                self.breach(exc)
            reply = {
                "id": request,
                "ok": False,
                "kind": "rejected",
                "message": f"{type(exc).__name__}: {exc}",
            }
        finally:
            with self._queries_lock:
                self._queries.pop(request, None)
        self.send(reply)


DATA_MARGIN_BYTES = 64 * 1024 * 1024


def data_limit(baseline: int, cap: int) -> int:
    return baseline + cap + DATA_MARGIN_BYTES


def data_segment_bytes() -> int | None:
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmData:"):
            return int(line.split()[1]) * 1024
    return None


def limit_data(cap: int) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    baseline = data_segment_bytes()
    if baseline is None:
        print("sluice store: could not read VmData; RLIMIT_DATA not set", file=sys.stderr)
        return None
    limit = data_limit(baseline, cap)
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    except ValueError, OSError:
        print(f"sluice store: could not set RLIMIT_DATA to {limit}", file=sys.stderr)
        return None
    return limit


def main(argv: list[str]) -> int:
    descriptor = int(argv[0])
    sys.stdout = sys.stderr
    channel = socket.socket(fileno=descriptor)
    Worker(channel).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
