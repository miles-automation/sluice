"""The `query` tool: gate, timeout, caps, rendering (spec 6)."""

import logging
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


def render(outcome: QueryOutcome, limits: Limits) -> str:
    """A markdown table plus an explicit account of everything withheld.

    Every truncation is stated. A silently truncated result is a correctness bug
    in a tool whose whole claim is that its answers are exact.
    """
    lines: list[str] = []
    if not outcome.columns:
        lines.append("(no columns)")
    else:
        header = "| " + " | ".join(outcome.columns) + " |"
        divider = "| " + " | ".join("---" for _ in outcome.columns) + " |"
        lines.append(header)
        lines.append(divider)
        used = len(header) + len(divider) + 2
        for row in outcome.rows:
            cells = []
            for value in row:
                rendered, cut = escape_cell(value, limits.max_cell_bytes)
                if cut:
                    outcome.truncated_cells += 1
                cells.append(rendered)
            line = "| " + " | ".join(cells) + " |"
            if used + len(line) + 1 > limits.query_max_bytes:
                outcome.dropped_rows = len(outcome.rows) - len(lines) + 2
                break
            used += len(line) + 1
            lines.append(line)

    shown = max(len(lines) - 2, 0) if outcome.columns else 0
    notes: list[str] = [f"{shown} row(s) shown."]
    if outcome.more_rows:
        # `max_rows + 1` proves at least one more row exists. It cannot give a
        # count without a second execution, so it must not claim one.
        notes.append("Additional rows exist beyond max_rows; the exact number is not known.")
    if outcome.dropped_rows > 0:
        notes.append(f"{outcome.dropped_rows} row(s) omitted to stay under the byte cap.")
    if outcome.truncated_cells > 0:
        notes.append(f"{outcome.truncated_cells} cell(s) truncated, marked with a trailing ….")
    lines.append("")
    lines.append(" ".join(notes))
    return "\n".join(lines)


class QueryTool:
    """Runs agent SQL on a connection of its own."""

    def __init__(self, store: Store, limits: Limits) -> None:
        self._store = store
        self._limits = limits

    async def run(self, sql: str, max_rows: int | None = None) -> str:
        limit = self._resolve_limit(max_rows)
        cursor = self._store.connection.cursor()
        try:
            check(sql, cursor, self._store.allowed_objects)
        except QueryRejectedError:
            cursor.close()
            raise
        try:
            outcome = await self._execute(cursor, sql, limit)
        finally:
            cursor.close()
        return render(outcome, self._limits)

    def _resolve_limit(self, max_rows: int | None) -> int:
        if max_rows is None:
            return self._limits.query_max_rows
        if max_rows < 1:
            raise QueryRejectedError(f"max_rows must be at least 1, got {max_rows}")
        return min(max_rows, self._limits.query_max_rows)

    async def _execute(
        self, cursor: duckdb.DuckDBPyConnection, sql: str, limit: int
    ) -> QueryOutcome:
        """Execute the SQL unmodified, with a watchdog on this exact connection.

        Unmodified matters: wrapping as `SELECT * FROM (<sql>) LIMIT n+1` breaks
        on trailing semicolons and on duplicate output column names, both of
        which the gate accepts. Rejecting SQL the agent was told is legal is
        worse than the cap it would buy.

        The watchdog interrupts `cursor`, which is the connection the query is
        actually running on. Interrupting its parent does nothing, measured, and
        would look like a working timeout because short queries finish anyway.
        """
        outcome = QueryOutcome()

        def blocking() -> tuple[list[str], list[tuple[Any, ...]]]:
            executed = cursor.execute(sql)
            description = executed.description or []
            columns = [str(column[0]) for column in description]
            return columns, executed.fetchmany(limit + 1)

        failure: QueryRejectedError | None = None
        columns: list[str] = []
        rows: list[tuple[Any, ...]] = []

        async with anyio.create_task_group() as group:

            async def watchdog() -> None:
                await anyio.sleep(self._limits.query_timeout_seconds)
                cursor.interrupt()

            group.start_soon(watchdog)
            try:
                columns, rows = await anyio.to_thread.run_sync(blocking)
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
                group.cancel_scope.cancel()

        # Raised out here, not inside the task group. anyio wraps anything
        # raised within a group into an ExceptionGroup, which slips straight
        # past the caller's `except QueryRejectedError` and crashes the tool call.
        if failure is not None:
            raise failure

        outcome.columns = columns
        outcome.more_rows = len(rows) > limit
        outcome.rows = rows[:limit]
        return outcome
