"""End-to-end aggregate correctness (spec 5.3, 5.5, and 5.6).

The generated data enters through the same text result channel as a downstream
MCP tool, is materialized by :class:`Interceptor`, and is read back through
the read-only :class:`QueryTool`.  The reference therefore applies the same
missing-key and JSON-null normalization as the product rather than inspecting
DuckDB's tables directly.
"""

import json
import math
import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import cast

import anyio
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import DrawFn
from mcp import types

from sluice.config import Limits
from sluice.intercept import Interceptor
from sluice.query import QueryTool
from sluice.store import Store

# Keep generated integer aggregates in the binary64-safe portion of the
# correctness domain.  The fixed table below separately pins large-int
# counterexamples where DuckDB's median loses precision.
_SAFE_INT = st.integers(min_value=-(2**53), max_value=2**53)
_FLOAT_SAFE_INT = st.integers(min_value=-(2**53), max_value=2**53)
_SAFE_FLOAT = st.floats(
    min_value=-1_000_000.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
    width=64,
)
_GROUP = st.sampled_from(["alpha", "beta", "gamma"])


@st.composite
def _row(draw: DrawFn) -> dict[str, object]:
    """A row with safe numeric columns and room for heterogeneous scalars."""
    return {
        "int_value": draw(st.one_of(_SAFE_INT, st.none())),
        # The numeric column deliberately mixes integer and float values.  Both
        # are in the exact binary64 domain required by §5.5.
        "float_value": draw(st.one_of(_FLOAT_SAFE_INT, _SAFE_FLOAT, st.none())),
        "group": draw(_GROUP),
    }


@st.composite
def _rows(draw: DrawFn) -> list[dict[str, object]]:
    """Generate 2-500 rows, guaranteeing missing, NULL, and mixed scalars."""
    rows = draw(st.lists(_row(), min_size=2, max_size=500))

    # Make the properties useful even for the smallest examples: the first
    # row has numeric values, the second mixes in an integer, and optional is
    # both absent and explicitly NULL.  The remaining rows stay fully random.
    rows[0]["int_value"] = draw(_SAFE_INT)
    rows[0]["float_value"] = draw(_SAFE_FLOAT)
    rows[1]["float_value"] = draw(_FLOAT_SAFE_INT)
    rows[0]["mixed_value"] = draw(_SAFE_INT)
    rows[1]["mixed_value"] = "a string"
    # `optional` is deliberately absent from the first row; this exercises
    # missing-key normalization.  The second row carries explicit JSON null.
    rows[1]["optional"] = None
    for row in rows[2:]:
        if draw(st.booleans()):
            row["optional"] = draw(st.one_of(_SAFE_INT, st.none()))
    return rows


def _table_name(result: types.CallToolResult) -> str:
    structured = result.structured_content
    assert isinstance(structured, dict)
    tables = structured.get("tables")
    assert isinstance(tables, list) and tables
    table = tables[0]
    assert isinstance(table, dict)
    name = table.get("name")
    assert isinstance(name, str)
    return name


def _table_columns(result: types.CallToolResult) -> dict[str, dict[str, object]]:
    structured = result.structured_content
    assert isinstance(structured, dict)
    tables = structured.get("tables")
    assert isinstance(tables, list) and tables
    table = tables[0]
    assert isinstance(table, dict)
    columns = table.get("columns")
    assert isinstance(columns, list)
    return {
        str(column["name"]): cast(dict[str, object], column)
        for column in columns
        if isinstance(column, dict) and "name" in column
    }


def _data_rows(markdown: str) -> list[list[str]]:
    """Read the simple, unescaped markdown rows emitted by QueryTool."""
    lines = [line for line in markdown.splitlines() if line.startswith("|")]
    assert len(lines) >= 3
    return [[cell.strip() for cell in line[1:-1].split("|")] for line in lines[2:]]


async def _materialize(
    interceptor: Interceptor,
    payload: object,
    tool: str = "generated_rows",
) -> types.CallToolResult:
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))]
    )
    intercepted = await interceptor.intercept(
        server="generated",
        tool=tool,
        mounted="generated__rows",
        arguments=None,
        result=result,
        meta=None,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    return intercepted


async def _assert_generated_aggregates(
    rows: list[dict[str, object]], *, mixed_exact: bool | None = False
) -> None:
    limits = Limits()
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits, query_available=True)
        query = QueryTool(store, limits)
        result = await _materialize(interceptor, {"items": rows})
        table = _table_name(result)
        columns = _table_columns(result)
        assert columns["int_value"]["type"] == "BIGINT"
        assert columns["int_value"]["exact"] is True
        assert columns["float_value"]["type"] == "DOUBLE"
        # DOUBLE aggregates have no universal exactness claim: summation order
        # can defeat even the documented tolerance under cancellation.
        assert columns["float_value"]["exact"] is False
        assert columns["mixed_value"]["type"] == "VARCHAR"
        if mixed_exact is not None:
            assert columns["mixed_value"]["exact"] is mixed_exact

        quoted = f'"{table}"'
        int_values = [
            value
            for row in rows
            if (value := row.get("int_value")) is not None
            and isinstance(value, int)
            and not isinstance(value, bool)
        ]
        float_values = [
            float(value)
            for row in rows
            if (value := row.get("float_value")) is not None
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ]
        optional_values = [row.get("optional") for row in rows]

        aggregate = _data_rows(
            await query.run(
                f"SELECT count(*), count(int_value), min(int_value), max(int_value), "
                f"count(DISTINCT int_value), sum(int_value), median(int_value), "
                f"count(optional), count(DISTINCT optional) FROM {quoted}"
            )
        )[0]
        assert int(aggregate[0]) == len(rows)
        assert int(aggregate[1]) == len(int_values)
        assert int(aggregate[2]) == min(int_values)
        assert int(aggregate[3]) == max(int_values)
        assert int(aggregate[4]) == len(set(int_values))
        assert int(aggregate[5]) == sum(int_values)
        assert float(aggregate[6]) == float(statistics.median(int_values))
        present_optional = [value for value in optional_values if value is not None]
        assert int(aggregate[7]) == len(present_optional)
        assert int(aggregate[8]) == len(set(present_optional))

        # These order statistics are still checked over generated bounded
        # values, but the handle's false exact flag prevents this from being a
        # promise to callers for arbitrary DOUBLE columns.
        float_aggregate = _data_rows(
            await query.run(
                f"SELECT count(float_value), min(float_value), max(float_value), "
                f"count(DISTINCT float_value), median(float_value) FROM {quoted}"
            )
        )[0]
        assert int(float_aggregate[0]) == len(float_values)
        assert float(float_aggregate[1]) == min(float_values)
        assert float(float_aggregate[2]) == max(float_values)
        assert int(float_aggregate[3]) == len(set(float_values))
        assert float(float_aggregate[4]) == statistics.median(float_values)

        grouped = _data_rows(
            await query.run(
                f'SELECT "group", count(*) FROM {quoted} GROUP BY "group" ORDER BY "group"'
            )
        )
        expected_groups = Counter(cast(str, row["group"]) for row in rows)
        assert [(row[0], int(row[1])) for row in grouped] == sorted(expected_groups.items())


@settings(max_examples=25, deadline=None)
@given(rows=_rows())
@pytest.mark.slow
def test_aggregates_match_the_normalized_source(rows: list[dict[str, object]]) -> None:
    """Fuzz materialization, inference, and query together."""
    anyio.run(_assert_generated_aggregates, rows)


_FIXED_CASES: tuple[tuple[list[object], str, float], ...] = (
    (
        [0, 2**63 - 2, 2**63 - 1],
        "median(value)",
        9.223372036854776e18,
    ),
    ([1e308, 1e308], "median(value)", 1e308),
    (
        [-1e308, 1.0, 2.0, 1e308],
        "avg(value)",
        0.0,
    ),
    ([9007199254740993, 0.5], "max(value)", 9007199254740992.0),
)


@pytest.mark.parametrize("values, expression, duck_value", _FIXED_CASES)
def test_out_of_domain_cases_are_explicitly_inexact(
    values: list[object], expression: str, duck_value: float
) -> None:
    """Pin the §5.6 counterexamples rather than relying on random discovery."""

    async def check() -> None:
        limits = Limits()
        with Store.open(limits) as store:
            interceptor = Interceptor(store, limits, query_available=True)
            query = QueryTool(store, limits)
            result = await _materialize(
                interceptor,
                {"items": [{"value": value} for value in values]},
                tool="fixed_counterexample",
            )
            columns = _table_columns(result)
            assert columns["value"]["exact"] is False
            text = cast(types.TextContent, result.content[0]).text
            assert "(inexact)" in text
            table = _table_name(result)
            row = _data_rows(await query.run(f'SELECT {expression} FROM "{table}"'))[0]
            observed = float(row[0])
            assert isinstance(duck_value, float)
            assert math.isclose(observed, duck_value, rel_tol=0.0, abs_tol=0.0)
            source = [
                value
                for value in values
                if isinstance(value, int | float) and not isinstance(value, bool)
            ]
            if expression == "median(value)":
                python_value = statistics.median(source)
            elif expression == "avg(value)":
                python_value = statistics.fmean(source)
            elif expression == "max(value)":
                python_value = max(source)
            else:
                raise AssertionError(f"unhandled fixed expression: {expression}")
            assert python_value != observed

    anyio.run(check)


def test_float_cancellation_is_a_bounded_regression_not_a_guarantee() -> None:
    """Even moderate cancellation can exceed both documented tolerances."""

    values: list[object] = [1e6, 1e-10, -1e6]

    async def check() -> None:
        limits = Limits()
        with Store.open(limits) as store:
            interceptor = Interceptor(store, limits, query_available=True)
            query = QueryTool(store, limits)
            result = await _materialize(
                interceptor,
                {"items": [{"value": value} for value in values]},
                tool="float_cancellation",
            )
            columns = _table_columns(result)
            assert columns["value"]["type"] == "DOUBLE"
            assert columns["value"]["exact"] is False
            table = _table_name(result)
            row = _data_rows(await query.run(f'SELECT avg(value), sum(value) FROM "{table}"'))[0]
            reference = [float(value) for value in values if isinstance(value, int | float)]
            assert not math.isclose(
                float(row[0]), statistics.fmean(reference), rel_tol=1e-9, abs_tol=1e-12
            )
            assert not math.isclose(float(row[1]), sum(reference), rel_tol=1e-9, abs_tol=1e-12)

    anyio.run(check)


@pytest.mark.parametrize("count", [1, 500])
def test_advertised_row_boundaries_are_materialized(count: int) -> None:
    """Pin the lower and upper row-count boundaries used by the design."""
    rows: list[dict[str, object]] = [
        {
            "int_value": index,
            "float_value": index + 0.5,
            "group": "alpha" if index % 2 == 0 else "beta",
            "mixed_value": "only" if count == 1 else index if index % 2 == 0 else "mixed",
            "optional": None if index == 0 else index,
        }
        for index in range(count)
    ]

    async def check() -> None:
        await _assert_generated_aggregates(rows, mixed_exact=None)

    anyio.run(check)
