"""The `query` tool: gate, timeout, caps, rendering (spec 6)."""

import logging
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import anyio
import duckdb

from sluice.config import QUERY_OUTPUT_MIN_BYTES, Limits
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
    if len(text.encode("utf-8")) <= max_bytes:
        return text, False
    marker = "…"
    marker_bytes = len(marker.encode("utf-8"))
    rendered, _ = truncate_to_bytes(text, max(max_bytes - marker_bytes, 0))
    return rendered + (marker if max_bytes >= marker_bytes else ""), True


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


class _DeadlineExpiredError(Exception):
    """Internal marker for a deadline that fired before a worker started."""


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
        evicted = self._store.evicted_tables
        outcome = QueryOutcome()
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            # The query may still be waiting for a worker or may have just
            # finished. Either way, avoid an unhandled Timer-thread traceback.
            with suppress(duckdb.Error):
                cursor.interrupt()

        def blocking() -> tuple[list[str], list[tuple[Any, ...]]]:
            if expired.is_set():
                raise _DeadlineExpiredError
            check(sql, cursor, allowed, evicted)
            if expired.is_set():
                raise _DeadlineExpiredError
            executed = cursor.execute(sql)
            description = executed.description or []
            fetched = executed.fetchmany(limit + 1)
            if expired.is_set():
                raise _DeadlineExpiredError
            return [str(column[0]) for column in description], fetched

        failure: QueryRejectedError | None = None
        columns: list[str] = []
        rows: list[tuple[Any, ...]] = []
        timer = threading.Timer(self._limits.query_timeout_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            columns, rows = await anyio.to_thread.run_sync(blocking)
        except duckdb.InterruptException, _DeadlineExpiredError:
            failure = QueryRejectedError(
                f"query exceeded the {self._limits.query_timeout_seconds}s timeout "
                "and was interrupted"
            )
        except QueryRejectedError as exc:
            failure = self._timeout_failure() if expired.is_set() else exc
        except duckdb.Error as exc:
            # Never an empty success: a failing query has to look like a
            # failure, not like a result set with no rows.
            failure = (
                self._timeout_failure()
                if expired.is_set()
                else QueryRejectedError(f"{type(exc).__name__}: {exc}")
            )
        finally:
            timer.cancel()
            timer.join()
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

    def _timeout_failure(self) -> QueryRejectedError:
        return QueryRejectedError(
            f"query exceeded the {self._limits.query_timeout_seconds}s timeout and was interrupted"
        )
