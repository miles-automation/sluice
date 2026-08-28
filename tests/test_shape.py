"""Extraction and depth-1 projection (spec 5.2, 5.3)."""

from typing import Any

import pytest

from sluice import shape


def test_bare_list_of_objects() -> None:
    result = shape.extract([{"a": 1}, {"a": 2}])
    assert [rs.source_path for rs in result.row_sets] == ["$"]
    assert result.row_sets[0].rows == [{"a": 1}, {"a": 2}]


def test_bare_list_of_scalars_gets_a_value_column() -> None:
    result = shape.extract([1, 2, 3])
    assert result.row_sets[0].rows == [{"value": 1}, {"value": 2}, {"value": 3}]


def test_every_candidate_array_is_materialized() -> None:
    """Picking the longest silently aggregates the wrong entity, and reporting
    the path afterward does not undo a wrong answer already given."""
    payload = {"rows": [{"amount": 1}] * 20, "facets": [{"count": 2}] * 100}
    result = shape.extract(payload)
    assert [rs.source_path for rs in result.row_sets] == ["$.rows", "$.facets"]


def test_mixed_elements_yield_no_table() -> None:
    """100% objects or nothing. A partial threshold leaves the remainder
    undefined and every implementer would handle it differently."""
    assert shape.extract([{"a": 1}, 42]).row_sets == []
    assert shape.extract([{"a": 1}, 42]).reason is not None
    nested = shape.extract({"items": [{"a": 1}, 42]})
    assert nested.row_sets == []
    assert nested.reason is not None
    assert "mixed_elements" in nested.reason


def test_empty_list_is_reported_not_silently_dropped() -> None:
    result = shape.extract({"items": []})
    assert result.row_sets == []
    assert result.reason is not None
    assert result.reason.startswith("empty")


def test_object_with_no_candidate_arrays_becomes_one_row() -> None:
    result = shape.extract({"id": 1, "nested": {"a": 1}})
    assert result.row_sets[0].rows == [{"id": 1, "nested": {"a": 1}}]


@pytest.mark.parametrize("payload", [42, "text", 3.5, True])
def test_scalars_yield_no_table(payload: Any) -> None:
    result = shape.extract(payload)
    assert result.row_sets == []
    assert result.reason is not None
    assert result.reason.startswith("scalar")


def test_projection_adds_a_row_ordinal() -> None:
    projection = shape.project([{"a": 1}, {"a": 2}], 64)
    assert [r[shape.ROW_COLUMN] for r in projection.records] == [0, 1]


def test_missing_keys_become_null() -> None:
    projection = shape.project([{"a": 1}, {"b": 2}], 64)
    assert projection.records[0]["b"] is None
    assert projection.records[1]["a"] is None


def test_missing_key_and_json_null_are_indistinguishable() -> None:
    """Deliberate and lossy. Spec 5.6 defines the correctness reference against
    this normalization rather than against raw JSON, which is the only way the
    property can hold."""
    projection = shape.project([{}, {"x": None}, {"x": 1}], 64)
    assert projection.records[0]["x"] is None
    assert projection.records[1]["x"] is None


def test_reserved_column_collisions_are_renamed() -> None:
    projection = shape.project([{"_row": "clash", "a": 1}], 64)
    assert projection.renamed == {"_row": "_row__src"}
    assert projection.records[0]["_row"] == 0
    assert projection.records[0]["_row__src"] == "clash"


def test_repeated_reserved_collisions_keep_allocating() -> None:
    projection = shape.project([{"_row": 1, "_row__src": 2}], 64)
    stored = set(projection.columns)
    assert len(stored) == 2
    assert shape.ROW_COLUMN not in stored


def test_column_cap_keeps_the_most_present_and_spills_the_rest() -> None:
    rows = [{"common": 1, "rare": 2}, {"common": 1}, {"common": 1}]
    projection = shape.project(rows, 1)
    assert projection.columns == ["common", shape.EXTRA_COLUMN]
    assert projection.extra_keys == ["rare"]
    assert projection.records[0][shape.EXTRA_COLUMN] == {"rare": 2}
    assert projection.records[1][shape.EXTRA_COLUMN] is None


def test_column_cap_ties_break_deterministically() -> None:
    """`wide(200)` gives 200 equally present keys. Without a tie-break the
    surviving column set is undefined."""
    rows = [{f"k{i:03d}": i for i in range(200)}]
    first = shape.project(rows, 64)
    second = shape.project(rows, 64)
    assert first.columns == second.columns
    assert first.columns[:3] == ["k000", "k001", "k002"]
