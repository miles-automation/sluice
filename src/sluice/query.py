"""The `query` tool: gate, timeout, caps, rendering (spec 6)."""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import anyio
import duckdb

from sluice.config import Limits
from sluice.gate import QueryRejectedError, check
from sluice.payload import truncate_to_bytes
from sluice.store import Store

logger = logging.getLogger(__name__)

QUERY_TOOL_NAME = "query"

QUERY_DESCRIPTION = (
    "Run one read-only SQL SELECT over the tables materialized from earlier tool "
    "results in this conversation. You can only reference tables named in the "
    "handles you were given; there is no way to list what exists. Column names "
    "come verbatim from the source payload, so quote any that are not plain "
    "identifiers. Columns marked JSON need json_extract and a cast before "
    "arithmetic, and columns marked inexact fall outside the range where results "
    "are guaranteed to match the source exactly."
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
    dropped_rows: int = 0


def escape_cell(value: object, max_bytes: int) -> tuple[str, bool]:
    """Render one value for a markdown table.

    Escaping is defined rather than left to the implementer, because two of
    these are ambiguous otherwise: SQL NULL and the string "NULL" would render
    identically, as would NULL and the empty string.
    """
    if value is None:
        return "NULL", False
    text = str(value)
    if text == "":
        return "''", False
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    rendered, cut = truncate_to_bytes(text, max_bytes)
    return (rendered + "…" if cut else rendered), cut


NOTES_RESERVE = 256
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
    if not outcome.columns:
        lines.append("(no columns)")
    else:
        # Headers are escaped and truncated like any other cell: they come from
        # the agent's own SQL aliases, which are arbitrary text.
        headers = [escape_cell(name, limits.max_cell_bytes)[0] for name in outcome.columns]
        header = "| " + " | ".join(headers) + " |"
        divider = "| " + " | ".join("---" for _ in headers) + " |"
        lines.append(header)
        lines.append(divider)
        used = _size(header) + _size(divider) + 2
        budget = max(limits.query_max_bytes - NOTES_RESERVE, 0)
        for index, row in enumerate(outcome.rows):
            cells = []
            for value in row:
                rendered, cut = escape_cell(value, limits.max_cell_bytes)
                if cut:
                    outcome.truncated_cells += 1
                cells.append(rendered)
            line = "| " + " | ".join(cells) + " |"
            if used + _size(line) + 1 > budget:
                outcome.dropped_rows = len(outcome.rows) - index
                break
            used += _size(line) + 1
            lines.append(line)
            shown += 1

    notes: list[str] = [f"{shown} row(s) shown."]
    if outcome.more_rows:
        # `max_rows + 1` proves at least one more row exists. It cannot give a
        # count without a second execution, so it must not claim one.
        notes.append("Additional rows exist beyond max_rows; the exact number is not known.")
    if outcome.dropped_rows > 0:
        notes.append(f"{outcome.dropped_rows} row(s) omitted to stay under the byte cap.")
    if outcome.truncated_cells > 0:
        notes.append(f"{outcome.truncated_cells} cell(s) truncated, marked with a trailing ….")
    text = "\n".join([*lines, "", " ".join(notes)])
    # Backstop: whatever happened above, the result never exceeds the cap.
    bounded, _ = truncate_to_bytes(text, limits.query_max_bytes)
    return bounded


class QueryTool:
    """Runs agent SQL on a connection of its own."""

    def __init__(self, store: Store, limits: Limits) -> None:
        self._store = store
        self._limits = limits

    async def run(self, sql: str, max_rows: int | None = None) -> str:
        limit = self._resolve_limit(max_rows)
        outcome = await self._execute(sql, limit)
        return render(outcome, self._limits)

    def _resolve_limit(self, max_rows: int | None) -> int:
        if max_rows is None:
            return self._limits.query_max_rows
        if max_rows < 1:
            raise QueryRejectedError(f"max_rows must be at least 1, got {max_rows}")
        return min(max_rows, self._limits.query_max_rows)

    async def _execute(self, sql: str, limit: int) -> QueryOutcome:
        """Gate and run on a connection of this query's own.

        Two things are deliberate.

        The deadline is a `threading.Timer`, not a task in a task group. A task
        group's watchdog is cancelled along with everything else when the client
        cancels the call, while the DuckDB worker ignores cancellation entirely,
        so the query then ran unbounded. A timer is immune to that: a cancelled
        query is still interrupted no later than the deadline.

        The gate runs **inside** the worker. `extract_statements` and
        `json_serialize_sql` are synchronous DuckDB calls, so running them on the
        event loop blocked the whole server on pathological SQL, and running them
        before the deadline started left them unbounded.
        """
        cursor = self._store.connection.cursor()
        allowed = self._store.allowed_objects
        outcome = QueryOutcome()

        def blocking() -> tuple[list[str], list[tuple[Any, ...]]]:
            check(sql, cursor, allowed)
            executed = cursor.execute(sql)
            description = executed.description or []
            return [str(column[0]) for column in description], executed.fetchmany(limit + 1)

        failure: QueryRejectedError | None = None
        columns: list[str] = []
        rows: list[tuple[Any, ...]] = []
        timer = threading.Timer(self._limits.query_timeout_seconds, cursor.interrupt)
        timer.daemon = True
        timer.start()
        try:
            columns, rows = await anyio.to_thread.run_sync(blocking)
        except QueryRejectedError as exc:
            failure = exc
        except duckdb.InterruptException:
            failure = QueryRejectedError(
                f"query exceeded the {self._limits.query_timeout_seconds}s timeout "
                "and was interrupted"
            )
        except duckdb.Error as exc:
            # Never an empty success: a failing query has to look like a
            # failure, not like a result set with no rows.
            failure = QueryRejectedError(f"{type(exc).__name__}: {exc}")
        finally:
            timer.cancel()
            # `abandon_on_cancel` is left at its default, so the worker has
            # finished by the time this runs even when the caller cancelled.
            # Closing the cursor out from under a live thread would be worse
            # than any timeout.
            cursor.close()

        if failure is not None:
            raise failure

        outcome.columns = columns
        outcome.more_rows = len(rows) > limit
        outcome.rows = rows[:limit]
        return outcome
