"""Value objects shared across the pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class PayloadChannel(StrEnum):
    STRUCTURED = "structured"
    TEXT = "text"
    NONE = "none"


class Passthrough(StrEnum):
    """Why a result was returned unmodified rather than replaced by a handle."""

    ERROR = "is_error"
    NON_TEXT = "non_text_content"
    OVERSIZE = "oversize"
    SELECTION_FAILED = "selection_failed"


@dataclass(frozen=True, slots=True)
class ColumnRef:
    name: str
    type: str
    exact: bool = True
    renamed_from: str | None = None


@dataclass(frozen=True, slots=True)
class TableRef:
    name: str
    source_path: str
    row_count: int
    columns: list[ColumnRef] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TablePlan:
    """A table computed but not yet written.

    Materialization is planned in full before anything touches the database, so
    the write can be one transaction rather than a sequence of steps that can
    half-succeed.
    """

    name: str
    source_path: str
    columns: list[Any]
    records: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SelectedPayload:
    """The payload materialization runs against, and where it came from."""

    channel: PayloadChannel
    value: Any = None
    text: str | None = None
    blocks: list[dict[str, Any]] = field(default_factory=list)
    structured: dict[str, Any] | None = None
    byte_size: int = 0
    wire_bytes: int = 0
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One envelope row (spec 3.1)."""

    call_id: str
    scope_id: str
    seq: int
    server: str
    tool: str
    args: dict[str, Any] | None
    payload: SelectedPayload
    is_error: bool
    failure_class: str | None
    content_kinds: list[str]
    started_at: datetime
    ended_at: datetime
    flat_tables: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    flat_reason: str | None = None
    retention_seq: int = 0

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class Handle:
    """What the agent gets in place of the payload (spec 4)."""

    call_id: str
    scope_id: str
    channel: PayloadChannel
    conflict: bool
    byte_size: int
    preview: str
    preview_complete: bool
    preview_rows: int | None = None
    total_rows: int | None = None
    tables: list[TableRef] = field(default_factory=list)
    flat_reason: str | None = None
    query_available: bool = False
    """Whether the `query` tool is mounted. False until plan M4, and the handle
    must not tell the agent to use a tool that does not exist yet."""
