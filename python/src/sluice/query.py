"""The `query` tool: gate, timeout, caps, rendering (spec 6)."""

import logging
from dataclasses import dataclass, field
from typing import Any

from sluice.config import QUERY_OUTPUT_MIN_BYTES, Limits
from sluice.execute import escape_cell as escape_cell
from sluice.gate import QueryRejectedError
from sluice.store import CommitSequencer
from sluice.text import truncate_to_bytes

logger = logging.getLogger(__name__)

QUERY_TOOL_NAME = "query"

QUERY_DESCRIPTION = (
    "Run one read-only SQL SELECT over the tables materialized from earlier tool "
    "results in this conversation. You can only reference tables named in the "
    "handles you were given; there is no way to list what exists. Column names "
    "come verbatim from the source payload, so quote any that are not plain "
    "identifiers. Columns marked JSON need json_extract and a cast before "
    "arithmetic, and columns marked inexact fall outside the range where results "
    "are guaranteed to match the source exactly. Older tables may be evicted by "
    "the session retention budget; rerun the source tool call if that happens."
)

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "A single SELECT statement."},
        "max_rows": {
            "type": "integer",
            "description": "Maximum rows to return.",
            "minimum": 1,
        },
    },
    "required": ["sql"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class QueryOutcome:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    more_rows: bool = False
    truncated_cells: int = 0
    truncated_headers: int = 0
    dropped_rows: int = 0


NOTES_RESERVE = QUERY_OUTPUT_MIN_BYTES
"""Bytes held back from the body so the truncation notices always fit. A result
that silently dropped its own "rows were omitted" line would be the exact bug
the notices exist to prevent."""


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def render(outcome: QueryOutcome, limits: Limits) -> str:
    """A markdown table plus an explicit account of everything withheld.

    The cap is counted in **bytes**, over the whole output including the header
    and the notes. Counting characters and excluding the header let a 100-byte
    cap return 195 bytes of Unicode, and a single long column alias return 5 KB.
    """
    lines: list[str] = []
    shown = 0
    dropped_rows = outcome.dropped_rows
    truncated_cells = outcome.truncated_cells
    truncated_headers = outcome.truncated_headers
    table_omitted = False
    body_budget = max(limits.query_max_bytes - NOTES_RESERVE - 2, 0)
    if not outcome.columns:
        if _size("(no columns)") <= body_budget:
            lines.append("(no columns)")
        else:
            table_omitted = True
    else:
        # Headers are escaped and truncated like any other cell: they come from
        # the agent's own SQL aliases, which are arbitrary text.
        header_parts = [escape_cell(name, limits.max_cell_bytes) for name in outcome.columns]
        headers = [part[0] for part in header_parts]
        header = "| " + " | ".join(headers) + " |"
        divider = "| " + " | ".join("---" for _ in headers) + " |"
        used = _size(header) + _size(divider) + 1
        if used > body_budget:
            table_omitted = True
            dropped_rows += len(outcome.rows)
        else:
            lines.extend((header, divider))
            truncated_headers += sum(cut for _, cut in header_parts)
            for index, row in enumerate(outcome.rows):
                rendered_cells = [escape_cell(value, limits.max_cell_bytes) for value in row]
                line = "| " + " | ".join(cell for cell, _ in rendered_cells) + " |"
                if used + _size(line) + 1 > body_budget:
                    dropped_rows += len(outcome.rows) - index
                    break
                used += _size(line) + 1
                truncated_cells += sum(cut for _, cut in rendered_cells)
                lines.append(line)
                shown += 1

    notes: list[str] = [f"{shown} row(s) shown."]
    if outcome.more_rows:
        # `max_rows + 1` proves at least one more row exists. It cannot give a
        # count without a second execution, so it must not claim one.
        notes.append("Additional rows exist beyond max_rows; the exact number is not known.")
    if dropped_rows > 0:
        notes.append(f"{dropped_rows} row(s) omitted to stay under the byte cap.")
    if truncated_cells > 0:
        notes.append(f"{truncated_cells} cell(s) truncated, marked with a trailing ….")
    if truncated_headers > 0:
        notes.append(f"{truncated_headers} column header(s) truncated, marked with a trailing ….")
    if table_omitted:
        notes.append("The table was omitted because its header exceeded the output byte cap.")
    notice = " ".join(notes)
    body = "\n".join(lines)
    text = f"{body}\n\n{notice}" if body else notice
    if _size(text) <= limits.query_max_bytes:
        return text
    # Defensive backstop. This path retains an explicit notice rather than
    # cutting from the end, where every truncation notice lives.
    fallback = "Output truncated to stay under the configured byte cap."
    prefix_limit = limits.query_max_bytes - _size(fallback) - 2
    prefix, _ = truncate_to_bytes(body, max(prefix_limit, 0))
    return f"{prefix}\n\n{fallback}" if prefix else fallback


class QueryTool:
    """Runs agent SQL on a connection of its own."""

    def __init__(self, store: CommitSequencer, limits: Limits) -> None:
        self._store = store
        self._limits = limits

    async def run(self, sql: str, max_rows: int | None = None) -> str:
        limit = self._resolve_limit(max_rows)
        result = await self._store.run_query(sql, limit, self._limits)
        outcome = QueryOutcome(
            columns=result.columns,
            rows=list(result.rows),
            more_rows=result.more_rows,
            dropped_rows=result.omitted,
        )
        return render(outcome, self._limits)

    def _resolve_limit(self, max_rows: int | None) -> int:
        if max_rows is None:
            return self._limits.query_max_rows
        if max_rows < 1:
            raise QueryRejectedError(f"max_rows must be at least 1, got {max_rows}")
        return min(max_rows, self._limits.query_max_rows)
