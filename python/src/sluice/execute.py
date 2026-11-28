import re
import threading
from dataclasses import dataclass

import duckdb

from sluice.gate import QueryRejectedError, check
from sluice.text import truncate_to_bytes

CELL_SLACK_BYTES = 4

type Cell = str | None


@dataclass(frozen=True, slots=True)
class QueryRows:
    columns: list[str]
    rows: list[tuple[Cell, ...]]


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
    return QueryRows(
        columns=[str(column[0]) for column in description],
        rows=[tuple(cell_text(value, cap) for value in row) for row in fetched],
    )
