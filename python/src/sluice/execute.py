import re
import threading
from dataclasses import dataclass

import duckdb

from sluice.config import QUERY_OUTPUT_MIN_BYTES
from sluice.gate import QueryRejectedError, check
from sluice.text import truncate_to_bytes

CELL_SLACK_BYTES = 4

type Cell = str | None


@dataclass(frozen=True, slots=True)
class QueryRows:
    columns: list[str]
    rows: list[tuple[Cell, ...]]
    more_rows: bool = False
    omitted: int = 0


class QueryInterruptedError(Exception):
    pass


class AllocationFailedError(QueryRejectedError):
    pass


_SUGGESTION_RE = re.compile(r"\n?Did you mean [^\n]*")

VANISHED_TABLE = "a table this query names is no longer available; it may have been evicted"


def is_allocation_failure(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    return isinstance(exc, duckdb.OutOfMemoryException) and "bad alloc" in str(exc).lower()


def engine_failure(exc: duckdb.Error) -> QueryRejectedError:
    message = str(exc)
    if isinstance(exc, duckdb.CatalogException) and "Table with name" in message:
        return QueryRejectedError(VANISHED_TABLE)
    text = f"{type(exc).__name__}: {_SUGGESTION_RE.sub('', message)}"
    if is_allocation_failure(exc):
        return AllocationFailedError(text)
    return QueryRejectedError(text)


def timeout_failure(seconds: float) -> QueryRejectedError:
    return QueryRejectedError(f"query exceeded the {seconds}s timeout and was interrupted")


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


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def bound_rows(
    columns: list[str],
    rows: list[tuple[Cell, ...]],
    limit: int,
    query_max_bytes: int,
    max_cell_bytes: int,
) -> QueryRows:
    more_rows = len(rows) > limit
    rows = rows[:limit]
    if not columns:
        return QueryRows(columns=columns, rows=rows, more_rows=more_rows)
    budget = max(query_max_bytes - QUERY_OUTPUT_MIN_BYTES - 2, 0)
    used = 3
    for count, name in enumerate(columns, start=1):
        used += _size(escape_cell(name, max_cell_bytes)[0]) + 9
        if used > budget:
            return QueryRows(
                columns=columns[:count], rows=[], more_rows=more_rows, omitted=len(rows)
            )
    for index, row in enumerate(rows):
        line = 4 + sum(_size(escape_cell(value, max_cell_bytes)[0]) for value in row)
        line += 3 * max(len(row) - 1, 0)
        if used + line + 1 > budget:
            return QueryRows(
                columns=columns,
                rows=rows[:index],
                more_rows=more_rows,
                omitted=len(rows) - index,
            )
        used += line + 1
    return QueryRows(columns=columns, rows=rows, more_rows=more_rows)


def cell_text(value: object, max_bytes: int) -> Cell:
    if value is None:
        return None
    text, _ = truncate_to_bytes(str(value)[: max_bytes + 1], max_bytes)
    return text


def execute_on_cursor(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    limit: int,
    allowed: set[str],
    evicted: set[str],
    cell_bytes: int,
    output_bytes: int,
    expired: threading.Event,
) -> QueryRows:
    try:
        if expired.is_set():
            raise QueryInterruptedError
        check(sql, cursor, allowed, evicted)
        if expired.is_set():
            raise QueryInterruptedError
        executed = cursor.execute(sql)
        description = executed.description or []
        fetched = executed.fetchmany(limit + 1)
        if expired.is_set():
            raise QueryInterruptedError
    except duckdb.InterruptException:
        raise QueryInterruptedError from None
    except QueryRejectedError:
        if expired.is_set():
            raise QueryInterruptedError from None
        raise
    except duckdb.Error as exc:
        if expired.is_set():
            raise QueryInterruptedError from None
        raise engine_failure(exc) from None
    cap = cell_bytes + CELL_SLACK_BYTES
    return bound_rows(
        [str(cell_text(column[0], cap)) for column in description],
        [tuple(cell_text(value, cap) for value in row) for row in fetched],
        limit,
        output_bytes,
        cell_bytes,
    )
