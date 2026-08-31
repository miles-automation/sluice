"""Column type inference (spec 5.5).

Three of these pin silent-wrongness cases. They are the reason the rules prefer
VARCHAR and a loud failure over a clever type and a quiet wrong answer.
"""

import pytest

from sluice.infer import ColumnType, coerce, infer_column


@pytest.mark.parametrize(
    ("values", "expected_type", "expected_exact"),
    [
        ([True, False, None], ColumnType.BOOLEAN, True),
        ([1, 2, 3], ColumnType.BIGINT, True),
        ([2**63 - 1, 0], ColumnType.BIGINT, False),
        ([2**64 + 1, 0], ColumnType.HUGEINT, False),
        ([2**127 - 1], ColumnType.HUGEINT, False),
        ([2**200, 1], ColumnType.VARCHAR, False),
        ([1.5, 2], ColumnType.DOUBLE, False),
        (["a", "b"], ColumnType.VARCHAR, True),
        ([{"a": 1}], ColumnType.JSON, False),
        ([[1, 2]], ColumnType.JSON, False),
        ([1, "oops"], ColumnType.VARCHAR, False),
        ([None, None], ColumnType.VARCHAR, True),
        ([], ColumnType.VARCHAR, True),
    ],
)
def test_inference_table(
    values: list[object], expected_type: ColumnType, expected_exact: bool
) -> None:
    assert infer_column(values) == (expected_type, expected_exact)


def test_mixed_scalars_become_varchar_never_json() -> None:
    """On a JSON column `median()` succeeds and returns a lexicographic answer.
    VARCHAR makes the same query fail instead of lying."""
    column_type, exact = infer_column([*range(300), "oops"])
    assert column_type is ColumnType.VARCHAR
    assert not exact


@pytest.mark.parametrize(
    "value",
    ["2026-01-01T00:00:00Z", "2026-01-01", "1.2.3", "2026-07-28"],
)
def test_timestamp_shaped_strings_stay_varchar(value: str) -> None:
    """DuckDB infers these as TIMESTAMP and returns them naive, dropping the
    offset. Version strings and opaque ids get swept up with them."""
    assert infer_column([value]) == (ColumnType.VARCHAR, True)


def test_integers_past_the_exact_float_range_are_marked_inexact() -> None:
    """Measured: `max([9007199254740993, 0.5])` as DOUBLE returns ...992.0."""
    column_type, exact = infer_column([9007199254740993, 0.5])
    assert column_type is ColumnType.DOUBLE
    assert not exact


def test_large_integer_columns_are_inexact_for_median() -> None:
    """DuckDB returns DOUBLE for median(BIGINT), losing large integer units."""
    column_type, exact = infer_column([0, 2**63 - 2, 2**63 - 1])
    assert column_type is ColumnType.BIGINT
    assert not exact


def test_large_finite_float_columns_are_inexact_for_tolerance_aggregates() -> None:
    """Large finite values can erase small terms during avg/sum cancellation."""
    column_type, exact = infer_column([-1e308, 1.0, 2.0, 1e308])
    assert column_type is ColumnType.DOUBLE
    assert not exact


def test_mixed_numeric_columns_are_never_claimed_exact() -> None:
    column_type, exact = infer_column([2**53, 0.5])
    assert column_type is ColumnType.DOUBLE
    assert not exact


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_floats_are_marked_inexact(value: float) -> None:
    column_type, exact = infer_column([1.0, value])
    assert column_type is ColumnType.DOUBLE
    assert not exact


def test_booleans_are_not_treated_as_integers() -> None:
    """bool subclasses int in Python, so the order of checks matters: without
    the bool test first, an all-boolean column would infer as BIGINT."""
    assert infer_column([True, False])[0] is ColumnType.BOOLEAN
    # And a column that is sometimes boolean and sometimes integer is genuinely
    # mixed, so it takes the conservative VARCHAR path rather than silently
    # coercing True to 1.
    assert infer_column([True, 1]) == (ColumnType.VARCHAR, False)


@pytest.mark.parametrize(
    ("value", "column_type", "expected"),
    [
        (None, ColumnType.BIGINT, None),
        ({"a": 1}, ColumnType.JSON, '{"a": 1}'),
        ("x", ColumnType.VARCHAR, "x"),
        (1, ColumnType.VARCHAR, "1"),
        (True, ColumnType.VARCHAR, "true"),
        (1, ColumnType.DOUBLE, 1.0),
        (1.0, ColumnType.BIGINT, 1),
    ],
)
def test_coerce(value: object, column_type: ColumnType, expected: object) -> None:
    assert coerce(value, column_type) == expected
