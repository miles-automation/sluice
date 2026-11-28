import json
import socket
import struct
from datetime import datetime
from typing import Any

from sluice.infer import ColumnSpec, ColumnType
from sluice.models import CallRecord, PayloadChannel, SelectedPayload, TablePlan

HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 2**31 - 1
REPLY_MAX_BYTES = 256 * 1024 * 1024

type Message = dict[str, Any]


class ChannelClosedError(EOFError):
    pass


def encode(message: Message) -> bytes:
    body = json.dumps(message, default=str, separators=(",", ":")).encode("ascii")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"message of {len(body)} bytes exceeds the {MAX_FRAME_BYTES} byte frame")
    return HEADER.pack(len(body)) + body


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    view = memoryview(bytearray(size))
    received = 0
    while received < size:
        count = sock.recv_into(view[received:], size - received)
        if count == 0:
            raise ChannelClosedError
        received += count
    return bytes(view)


def read_message(sock: socket.socket, max_bytes: int = MAX_FRAME_BYTES) -> Message:
    (size,) = HEADER.unpack(_read_exactly(sock, HEADER.size))
    if size > max_bytes:
        raise ValueError(f"frame of {size} bytes exceeds the {max_bytes} byte limit")
    decoded = json.loads(_read_exactly(sock, size))
    if not isinstance(decoded, dict):
        raise ValueError("frame is not a JSON object")
    return decoded


def _payload_to_wire(payload: SelectedPayload) -> Message:
    return {
        "channel": str(payload.channel),
        "value": payload.value,
        "text": payload.text,
        "blocks": payload.blocks,
        "structured": payload.structured,
        "byte_size": payload.byte_size,
        "wire_bytes": payload.wire_bytes,
        "conflict": payload.conflict,
    }


def _payload_from_wire(data: Message) -> SelectedPayload:
    return SelectedPayload(
        channel=PayloadChannel(data["channel"]),
        value=data["value"],
        text=data["text"],
        blocks=data["blocks"],
        structured=data["structured"],
        byte_size=data["byte_size"],
        wire_bytes=data["wire_bytes"],
        conflict=data["conflict"],
    )


def record_to_wire(record: CallRecord) -> Message:
    return {
        "call_id": record.call_id,
        "scope_id": record.scope_id,
        "seq": record.seq,
        "server": record.server,
        "tool": record.tool,
        "args": record.args,
        "payload": _payload_to_wire(record.payload),
        "is_error": record.is_error,
        "failure_class": record.failure_class,
        "content_kinds": record.content_kinds,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "flat_tables": record.flat_tables,
        "source_paths": record.source_paths,
        "flat_reason": record.flat_reason,
        "retention_seq": record.retention_seq,
    }


def record_from_wire(data: Message) -> CallRecord:
    return CallRecord(
        call_id=data["call_id"],
        scope_id=data["scope_id"],
        seq=data["seq"],
        server=data["server"],
        tool=data["tool"],
        args=data["args"],
        payload=_payload_from_wire(data["payload"]),
        is_error=data["is_error"],
        failure_class=data["failure_class"],
        content_kinds=data["content_kinds"],
        started_at=datetime.fromisoformat(data["started_at"]),
        ended_at=datetime.fromisoformat(data["ended_at"]),
        flat_tables=data["flat_tables"],
        source_paths=data["source_paths"],
        flat_reason=data["flat_reason"],
        retention_seq=data["retention_seq"],
    )


def plan_to_wire(plan: TablePlan) -> Message:
    columns: list[ColumnSpec] = plan.columns
    return {
        "name": plan.name,
        "source_path": plan.source_path,
        "columns": [
            [column.name, str(column.type), column.exact, column.renamed_from] for column in columns
        ],
        "records": plan.records,
    }


def plan_from_wire(data: Message) -> TablePlan:
    return TablePlan(
        name=data["name"],
        source_path=data["source_path"],
        columns=[
            ColumnSpec(name=name, type=ColumnType(kind), exact=exact, renamed_from=renamed)
            for name, kind, exact, renamed in data["columns"]
        ],
        records=data["records"],
    )


BREACH_EXIT = 86
