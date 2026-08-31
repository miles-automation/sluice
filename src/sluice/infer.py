"""Column type inference (spec 5.5).

Pure module: no DuckDB imports, no MCP imports, no IO.

Sluice owns inference because the engine lockdown blocks `read_json` (spec 5.4),
so DuckDB never sees the raw payload. Every rule here is deliberately
conservative: it prefers VARCHAR and a loud failure over a clever type and a
quiet wrong answer.
"""

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
INT128_MIN = -(2**127)
INT128_MAX = 2**127 - 1
EXACT_FLOAT_MAX = 2**53
"""Integer magnitude boundary for numeric exactness.

DuckDB's ``median`` returns ``DOUBLE`` for integer columns, so an integer
outside this range cannot retain exactness through every aggregate.  The same
boundary is not sufficient for floating-point columns: condition-dependent
rounding can defeat even the §5.6 average/sum tolerance at modest magnitudes.
Floating-point columns are consequently always marked non-exact.
"""


class ColumnType(StrEnum):
    BOOLEAN = "BOOLEAN"
    BIGINT = "BIGINT"
    HUGEINT = "HUGEINT"
    DOUBLE = "DOUBLE"
    VARCHAR = "VARCHAR"
    JSON = "JSON"


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type: ColumnType
    exact: bool
    renamed_from: str | None = None


def _is_bool(value: object) -> bool:
    # bool is a subclass of int, so this check must come first everywhere.
    return isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_float(value: object) -> bool:
    return isinstance(value, float)


def infer_column(values: list[Any]) -> tuple[ColumnType, bool]:
    """Return `(type, exact)` for one column's complete set of values.

    `exact` false means the column falls outside the domain where spec 5.6's
    correctness guarantee holds, and the handle says so per column.
    """
    present = [v for v in values if v is not None]
    if not present:
        return ColumnType.VARCHAR, True

    if any(isinstance(v, dict | list) for v in present):
        # Nested values become JSON, which is an aggregation trap: sum and avg
        # raise, but median succeeds and returns a lexicographic answer.
        return ColumnType.JSON, False

    if all(_is_bool(v) for v in present):
        return ColumnType.BOOLEAN, True

    if all(isinstance(v, str) for v in present):
        # Never inferred as TIMESTAMP even when ISO-8601 shaped: DuckDB returns
        # those naive, dropping the offset, and version strings and opaque ids
        # that happen to look like dates get swept up with them.
        return ColumnType.VARCHAR, True

    numeric = [v for v in present if _is_int(v) or _is_float(v)]
    if len(numeric) != len(present):
        # Mixed scalar types. VARCHAR rather than JSON, deliberately: on a JSON
        # column `median()` succeeds and returns a number-shaped string that is
        # not the median of anything, while VARCHAR makes the same query fail.
        return ColumnType.VARCHAR, False

    floats = [v for v in numeric if _is_float(v)]
    ints = [v for v in numeric if _is_int(v)]

    if not floats:
        if all(INT64_MIN <= v <= INT64_MAX for v in ints):
            exact = all(abs(v) <= EXACT_FLOAT_MAX for v in ints)
            return ColumnType.BIGINT, exact
        if all(INT128_MIN <= v <= INT128_MAX for v in ints):
            return ColumnType.HUGEINT, False
        return ColumnType.VARCHAR, False

    if any(not math.isfinite(v) for v in floats):
        return ColumnType.DOUBLE, False
    # A single column-level flag cannot encode the operation-dependent error
    # behavior of floating point aggregates. Keep the physical DOUBLE type,
    # but never claim universal exactness for it.
    return ColumnType.DOUBLE, False


def coerce(value: Any, column_type: ColumnType) -> Any:
    """Convert a projected value into what DuckDB accepts for its column."""
    if value is None:
        return None
    if column_type is ColumnType.JSON:
        return json.dumps(value, default=str)
    if column_type is ColumnType.VARCHAR:
        if isinstance(value, str):
            return value
        if _is_bool(value):
            return "true" if value else "false"
        return json.dumps(value, default=str)
    if column_type is ColumnType.BOOLEAN:
        return bool(value)
    if column_type is ColumnType.DOUBLE:
        return float(value)
    return int(value)
