import ctypes
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import anyio

from sluice.config import Limits
from sluice.execute import QueryRows, timeout_failure
from sluice.gate import GateContractError, QueryRejectedError
from sluice.ipc import (
    BREACH_EXIT,
    ChannelClosedError,
    Message,
    encode,
    plan_to_wire,
    read_message,
    record_to_wire,
)
from sluice.models import CallRecord, TablePlan
from sluice.store import CommitOutcome, CommitSequencer

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 0.02
INTERRUPT_GRACE_SECONDS = 5.0
OPEN_TIMEOUT_SECONDS = 60.0
CLOSE_TIMEOUT_SECONDS = 5.0
REPLY_MAX_BYTES = 256 * 1024 * 1024

_TOKEN = re.compile(r"[a-z0-9_]+")

type Sampler = Callable[[int], int | None]


def _darwin_sampler() -> Sampler:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")

    def sample(pid: int) -> int | None:
        buffer = (ctypes.c_uint64 * 64)()
        if libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(2), buffer) != 0:
            return None
        return max(int(buffer[8]), int(buffer[9]))

    return sample


def _linux_sampler() -> Sampler:
    page = os.sysconf("SC_PAGE_SIZE")

    def sample(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/statm", encoding="ascii") as handle:
                return int(handle.read().split()[1]) * page
        except OSError, IndexError, ValueError:
            return None

    return sample


def memory_sampler() -> Sampler | None:
    if sys.platform == "darwin":
        return _darwin_sampler()
    if sys.platform.startswith("linux"):
        return _linux_sampler()
    return None


def describe_bytes(value: int) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= size:
            amount = value / size
            return f"{amount:.0f} {unit}" if amount == int(amount) else f"{amount:.1f} {unit}"
    return f"{value} B"


def reset_message(cause: str, limits: Limits) -> str:
    return (
        f"store reset: {cause}. The store process is capped at store_max_memory = "
        f"{limits.store_max_memory} bytes ({describe_bytes(limits.store_max_memory)}); it was "
        "restarted, so every table and handle from before the reset is gone. Rerun the "
        "source tool calls to materialize their results again."
    )


def stale_message(name: str, limits: Limits) -> str:
    return (
        f"{name} was lost when the store was reset (store_max_memory = "
        f"{describe_bytes(limits.store_max_memory)}); its data is gone. Rerun the source "
        "tool call."
    )


class StoreDiedError(Exception):
    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class StoreResetError(RuntimeError):
    pass


class StoreWorkerError(RuntimeError):
    pass


@dataclass(slots=True)
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    reply: Message | None = None
    cause: str | None = None


class StoreProcess:
    def __init__(
        self,
        limits: Limits,
        generation: int,
        on_exit: Callable[[StoreProcess, str], None],
    ) -> None:
        self.generation = generation
        self.peak_bytes = 0
        self._limits = limits
        self._on_exit = on_exit
        self._sampler = memory_sampler()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._busy = threading.Condition(self._lock)
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._cause: str | None = None
        self._breach: str | None = None
        self._closing = False
        self._exited = False
        parent, child = socket.socketpair()
        self._channel = parent
        try:
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sluice._store_worker",
                    str(child.fileno()),
                    str(limits.store_max_memory),
                ],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=2,
                close_fds=True,
            )
        finally:
            child.close()
        try:
            self._open()
        except BaseException:
            self._process.kill()
            self._process.wait()
            parent.close()
            raise
        self._reader = threading.Thread(target=self._read, name="sluice-store-reader", daemon=True)
        self._watcher = threading.Thread(
            target=self._watch, name="sluice-store-memory", daemon=True
        )
        self._reader.start()
        self._watcher.start()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def alive(self) -> bool:
        with self._lock:
            return not self._exited and self._cause is None

    def _open(self) -> None:
        self._channel.settimeout(OPEN_TIMEOUT_SECONDS)
        fields = {name: getattr(self._limits, name) for name in Limits.__dataclass_fields__}
        self._channel.sendall(
            encode({"op": "open", "generation": self.generation, "limits": fields})
        )
        try:
            reply = read_message(self._channel, REPLY_MAX_BYTES)
        except (ChannelClosedError, OSError) as exc:
            code = self._process.wait(timeout=CLOSE_TIMEOUT_SECONDS)
            raise RuntimeError(f"the store process failed to start ({_exit_text(code)})") from exc
        self._channel.settimeout(None)
        if reply.get("ok"):
            return
        message = str(reply.get("message"))
        if reply.get("error") == "GateContractError":
            raise GateContractError(message)
        raise RuntimeError(f"the store process failed to open: {reply.get('error')}: {message}")

    def start(self, message: Message) -> tuple[int, _Pending]:
        pending = _Pending()
        with self._lock:
            if self._exited or self._cause is not None:
                raise StoreDiedError(self._cause or "the store process has exited")
            request = self._next_id
            self._next_id += 1
            self._pending[request] = pending
            self._busy.notify_all()
        frame = encode({**message, "id": request})
        try:
            with self._send_lock:
                self._channel.sendall(frame)
        except OSError:
            pending.done.wait()
        return request, pending

    def wait(self, request: int, pending: _Pending) -> Message:
        pending.done.wait()
        with self._lock:
            self._pending.pop(request, None)
        if pending.reply is None:
            raise StoreDiedError(pending.cause or "the store process has exited")
        return pending.reply

    def request(self, message: Message) -> Message:
        request, pending = self.start(message)
        return self.wait(request, pending)

    def notify(self, message: Message) -> None:
        frame = encode(message)
        try:
            with self._send_lock:
                self._channel.sendall(frame)
        except OSError:
            return

    def kill(self, cause: str) -> None:
        with self._lock:
            if self._exited or self._cause is not None:
                return
            self._cause = cause
        try:
            self._process.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            return

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._busy.notify_all()
        with suppress(OSError):
            self._channel.shutdown(socket.SHUT_RDWR)
        try:
            self._process.wait(timeout=CLOSE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._reader.join()
        self._watcher.join()
        self._channel.close()

    def _read(self) -> None:
        while True:
            try:
                message = read_message(self._channel, REPLY_MAX_BYTES)
            except ChannelClosedError, OSError, ValueError:
                break
            breach = message.get("breach")
            if breach is not None:
                with self._lock:
                    self._breach = str(breach)
                continue
            with self._lock:
                pending = self._pending.get(int(message.get("id", -1)))
            if pending is not None:
                pending.reply = message
                pending.done.set()
        self._process.kill()
        code = self._process.wait()
        with self._lock:
            closing = self._closing
            if closing:
                cause = "the store was closed"
            elif self._cause is not None:
                cause = self._cause
            elif self._breach is not None:
                cause = self._breach
            else:
                cause = f"the store process exited unexpectedly ({_exit_text(code)})"
            self._cause = cause
            self._exited = True
            pending_calls = list(self._pending.values())
            self._busy.notify_all()
        if not closing:
            logger.error("store process %s ended: %s", self._process.pid, cause)
            self._on_exit(self, cause)
        for pending in pending_calls:
            pending.cause = cause
            pending.done.set()

    def _watch(self) -> None:
        sampler = self._sampler
        cap = self._limits.store_max_memory
        while True:
            with self._lock:
                while not self._pending and not self._exited and not self._closing:
                    self._busy.wait()
                if self._exited or self._closing:
                    return
            if sampler is not None:
                used = sampler(self._process.pid)
                if used is not None:
                    self.peak_bytes = max(self.peak_bytes, used)
                    if used > cap:
                        self.kill(
                            f"the store process reached {describe_bytes(used)}, over its "
                            "memory cap, and was killed"
                        )
            with self._lock:
                self._busy.wait(SAMPLE_INTERVAL_SECONDS)


def _exit_text(code: int | None) -> str:
    if code is None:
        return "still running"
    if code < 0:
        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    if code == BREACH_EXIT:
        return "it reported an allocation failure"
    return f"exit status {code}"


class _BoundedNames:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._names: dict[str, None] = {}

    def add(self, names: list[str]) -> None:
        for name in names:
            folded = name.lower()
            self._names.pop(folded, None)
            self._names[folded] = None
        while len(self._names) > self._limit:
            del self._names[next(iter(self._names))]

    def take(self) -> list[str]:
        names = list(self._names)
        self._names.clear()
        return names

    def first_of(self, tokens: set[str]) -> str | None:
        for token in tokens:
            if token in self._names:
                return token
        return None


class SupervisedStore(CommitSequencer):
    def __init__(self, limits: Limits) -> None:
        super().__init__()
        self._limits = limits
        self._state = threading.Lock()
        self._spawn = threading.Lock()
        self._process: StoreProcess | None = None
        self._generation = 0
        self._resets = 0
        self._closed = False
        self._known = _BoundedNames(limits.max_session_calls * 64)
        self._lost = _BoundedNames(limits.max_session_calls * 64)

    @classmethod
    def open(cls, limits: Limits) -> Self:
        store = cls(limits)
        store._ensure()
        return store

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def generation(self) -> int:
        with self._state:
            return self._generation

    @property
    def resets(self) -> int:
        with self._state:
            return self._resets

    @property
    def process(self) -> StoreProcess | None:
        with self._state:
            return self._process

    def close(self) -> None:
        with self._spawn:
            with self._state:
                self._closed = True
                process = self._process
                self._process = None
            if process is not None:
                process.close()

    def _ensure(self) -> StoreProcess:
        with self._spawn:
            with self._state:
                if self._closed:
                    raise RuntimeError("the store is closed")
                current = self._process
                generation = self._generation
            if current is not None:
                if current.alive:
                    return current
                self._exited(current, "")
                with self._state:
                    generation = self._generation
            process = StoreProcess(self._limits, generation, self._exited)
            with self._state:
                self._process = process
            return process

    def _exited(self, process: StoreProcess, cause: str) -> None:
        with self._state:
            if process is not self._process:
                return
            self._process = None
            self._generation += 1
            self._resets += 1
            self._lost.add(self._known.take())

    def _remember(self, generation: int, names: list[str]) -> None:
        with self._state:
            if generation == self._generation:
                self._known.add(names)
            else:
                self._lost.add(names)

    async def _write(self, record: CallRecord, plans: list[TablePlan]) -> CommitOutcome:
        return await anyio.to_thread.run_sync(self._commit_blocking, record, plans)

    def _commit_blocking(self, record: CallRecord, plans: list[TablePlan]) -> CommitOutcome:
        process = self._ensure()
        message: Message = {
            "op": "commit",
            "record": record_to_wire(record),
            "plans": [plan_to_wire(plan) for plan in plans],
        }
        try:
            reply = process.request(message)
        except StoreDiedError as died:
            raise StoreResetError(reset_message(died.cause, self._limits)) from None
        if not reply.get("ok"):
            raise StoreWorkerError(f"{reply.get('error')}: {reply.get('message')}")
        retained = bool(reply["retained"])
        envelope = str(reply["envelope"])
        names = [plan.name for plan in plans] if retained else []
        self._remember(process.generation, [*names, envelope])
        return CommitOutcome(retained=retained, envelope=envelope)

    async def run_query(self, sql: str, limit: int, limits: Limits) -> QueryRows:
        with self._state:
            lost = self._lost.first_of(set(_TOKEN.findall(sql.lower())))
        if lost is not None:
            raise QueryRejectedError(stale_message(lost, self._limits))
        return await anyio.to_thread.run_sync(self._query_blocking, sql, limit, limits)

    def _query_blocking(self, sql: str, limit: int, limits: Limits) -> QueryRows:
        try:
            process = self._ensure()
        except Exception as exc:
            raise QueryRejectedError(f"the store is unavailable: {exc}") from None
        expired = threading.Event()
        timers: list[threading.Timer] = []
        try:
            request, pending = process.start(
                {"op": "query", "sql": sql, "limit": limit, "cell_bytes": limits.max_cell_bytes}
            )
        except StoreDiedError as died:
            raise QueryRejectedError(reset_message(died.cause, self._limits)) from None

        def give_up() -> None:
            process.kill(
                f"the query did not stop within {INTERRUPT_GRACE_SECONDS:g}s of its "
                f"{limits.query_timeout_seconds}s timeout interrupt"
            )

        def expire() -> None:
            expired.set()
            process.notify({"op": "interrupt", "target": request})
            grace = threading.Timer(INTERRUPT_GRACE_SECONDS, give_up)
            grace.daemon = True
            timers.append(grace)
            grace.start()

        deadline = threading.Timer(limits.query_timeout_seconds, expire)
        deadline.daemon = True
        deadline.start()
        try:
            reply = process.wait(request, pending)
        except StoreDiedError as died:
            raise QueryRejectedError(reset_message(died.cause, self._limits)) from None
        finally:
            deadline.cancel()
            deadline.join()
            for timer in timers:
                timer.cancel()
        if reply.get("ok") and not expired.is_set():
            rows: list[list[Any]] = reply["rows"]
            return QueryRows(columns=list(reply["columns"]), rows=[tuple(row) for row in rows])
        if expired.is_set() or reply.get("kind") == "interrupted":
            raise timeout_failure(limits.query_timeout_seconds)
        raise QueryRejectedError(str(reply.get("message")))
