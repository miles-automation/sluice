"""Extraction and depth-1 projection (spec 5.2, 5.3).

Pure module: no DuckDB imports, no MCP imports, no IO.
"""

from dataclasses import dataclass, field
from typing import Any

ROW_COLUMN = "_row"
CALL_COLUMN = "_call_id"
EXTRA_COLUMN = "_extra"
VALUE_COLUMN = "value"

RESERVED = (ROW_COLUMN, CALL_COLUMN, EXTRA_COLUMN)


@dataclass(frozen=True, slots=True)
class RowSet:
    source_path: str
    rows: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Extraction:
    row_sets: list[RowSet] = field(default_factory=list)
    reason: str | None = None
    """Why there are no row sets. None when there are."""


def _all_objects(items: list[Any]) -> bool:
    return bool(items) and all(isinstance(item, dict) for item in items)


def _all_scalars(items: list[Any]) -> bool:
    return bool(items) and all(not isinstance(item, dict | list) for item in items)


def extract(payload: Any) -> Extraction:
    """Find the row sets in a parsed payload.

    Real MCP results are rarely a bare array. The common shape is an envelope
    object like `{"items": [...], "next_cursor": ...}`, so finding the rows is
    its own step rather than a special case of flattening.
    """
    if isinstance(payload, list):
        if not payload:
            return Extraction(reason="empty: the payload is an empty list")
        if _all_objects(payload):
            return Extraction([RowSet("$", payload)])
        if _all_scalars(payload):
            rows = [{VALUE_COLUMN: item} for item in payload]
            return Extraction([RowSet("$", rows)])
        # 100% or nothing. A 90% threshold leaves the remaining elements
        # undefined, and implementers would variously drop, wrap, or fail on
        # them, each changing count(*).
        return Extraction(reason="mixed_elements: the list mixes objects and non-objects")

    if isinstance(payload, dict):
        candidates = [
            (key, value)
            for key, value in payload.items()
            if isinstance(value, list) and _all_objects(value)
        ]
        if candidates:
            # Every candidate is materialized. Picking the longest silently
            # aggregates the wrong entity on a payload like
            # {"rows": [...20], "facets": [...100]}, and reporting the path
            # afterward does not undo a wrong answer already given.
            return Extraction([RowSet(f"$.{key}", value) for key, value in candidates])
        # A list that holds some objects but not only objects is an ambiguous
        # row set, not an ordinary value. Falling through to "the object itself
        # is one row" would bury it in a single JSON column and report success.
        ambiguous = [
            key
            for key, value in payload.items()
            if isinstance(value, list) and any(isinstance(item, dict) for item in value)
        ]
        if ambiguous:
            paths = ", ".join(f"$.{key}" for key in ambiguous)
            return Extraction(reason=f"mixed_elements: {paths} mixes objects and non-objects")
        empty_lists = [
            key for key, value in payload.items() if isinstance(value, list) and not value
        ]
        if empty_lists:
            paths = ", ".join(f"$.{key}" for key in empty_lists)
            return Extraction(reason=f"empty: {paths} had 0 rows")
        return Extraction([RowSet("$", [payload])])

    return Extraction(reason=f"scalar: the payload is a bare {type(payload).__name__}")


@dataclass(frozen=True, slots=True)
class Projection:
    """Depth-1 projected records, keyed by stored column name."""

    columns: list[str]
    records: list[dict[str, Any]]
    renamed: dict[str, str] = field(default_factory=dict)
    extra_keys: list[str] = field(default_factory=list)

    @property
    def has_extra(self) -> bool:
        return bool(self.extra_keys)


def _stored_name(key: str, taken: set[str]) -> str:
    """Column names are the source keys verbatim; generated SQL quotes them.

    Sanitizing would not be injective and would leave the agent unable to tell
    which source key a column came from. JSON object keys are already unique, so
    the only possible collision is with Sluice's own reserved columns.
    """
    if key not in RESERVED and key not in taken:
        return key
    candidate = f"{key}__src"
    suffix = 2
    while candidate in taken or candidate in RESERVED:
        candidate = f"{key}__src{suffix}"
        suffix += 1
    return candidate


def project(rows: list[dict[str, Any]], max_columns: int) -> Projection:
    """Depth-1 projection with a bounded column count (spec 5.3).

    Nothing deeper than depth 1 becomes a column: the column list is printed
    into the agent's context on every call, and a fully inferred nested schema
    can be larger than the preview it replaces.

    Missing keys and JSON `null` both become SQL NULL. That is deliberate and
    lossy, and it is why spec 5.6 defines the correctness reference against this
    normalization rather than against the raw JSON.
    """
    presence: dict[str, int] = {}
    order: dict[str, int] = {}
    for index, row in enumerate(rows):
        for key in row:
            presence[key] = presence.get(key, 0) + 1
            order.setdefault(key, index * 10_000 + len(order))

    ranked = sorted(presence, key=lambda k: (-presence[k], order[k]))
    kept = ranked[:max_columns]
    extra_keys = ranked[max_columns:]
    # Restore payload order for the columns that survived, so the handle reads
    # the way the data does.
    kept.sort(key=lambda k: order[k])

    renamed: dict[str, str] = {}
    taken: set[str] = set()
    stored: dict[str, str] = {}
    for key in kept:
        name = _stored_name(key, taken)
        taken.add(name)
        stored[key] = name
        if name != key:
            renamed[key] = name

    columns = [stored[key] for key in kept]
    if extra_keys:
        columns.append(EXTRA_COLUMN)

    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        record: dict[str, Any] = {ROW_COLUMN: index}
        for key in kept:
            record[stored[key]] = row.get(key)
        if extra_keys:
            leftover = {key: row[key] for key in extra_keys if key in row}
            record[EXTRA_COLUMN] = leftover or None
        records.append(record)

    return Projection(columns=columns, records=records, renamed=renamed, extra_keys=extra_keys)
