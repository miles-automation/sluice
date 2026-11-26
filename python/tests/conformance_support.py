import copy
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

CONFORMANCE_ROOT = Path(__file__).resolve().parents[2] / "conformance"
FIXTURES = CONFORMANCE_ROOT / "fixtures"
SCHEMA = CONFORMANCE_ROOT / "schema.json"

type Case = dict[str, Any]

LOCKDOWN: tuple[str, ...] = (
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    "SET lock_configuration = true",
)


@dataclass(frozen=True, slots=True)
class Fixture:
    path: Path
    kind: str
    defaults: dict[str, Any]
    cases: list[Case]


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_fixtures() -> list[Fixture]:
    validator = _validator()
    fixtures: list[Fixture] = []
    seen: dict[str, Path] = {}
    for path in sorted(FIXTURES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            details = "; ".join(
                f"{'/'.join(map(str, error.path))}: {error.message}" for error in errors[:5]
            )
            raise ValueError(f"{path.name} does not match conformance/schema.json: {details}")
        for case in document["cases"]:
            case_id = case["id"]
            if case_id in seen:
                raise ValueError(
                    f"duplicate case id {case_id!r} in {path.name} and {seen[case_id]}"
                )
            seen[case_id] = path
        fixtures.append(
            Fixture(
                path=path,
                kind=document["kind"],
                defaults=document.get("defaults", {}),
                cases=document["cases"],
            )
        )
    if not fixtures:
        raise ValueError(f"no fixtures found under {FIXTURES}")
    return fixtures


FIXTURE_SET = load_fixtures()


def params(kind: str) -> list[Any]:
    return [
        pytest.param({**fixture.defaults, **case}, id=case["id"])
        for fixture in FIXTURE_SET
        if fixture.kind == kind
        for case in fixture.cases
    ]


_GATE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("could not parse", "parse_error"),
    ("no statement found", "no_statement"),
    ("statements found", "multiple_statements"),
    ("only SELECT is allowed", "not_select"),
    ("could not be analyzed", "unanalyzable"),
    ("table function not allowed", "table_function"),
    ("function not allowed", "function"),
    ("schema-qualified", "schema_qualified"),
    ("was evicted", "evicted_table"),
    ("unknown table", "unknown_table"),
    ("SHOW and DESCRIBE", "show_describe"),
    ("max_rows must be", "invalid_argument"),
    ("needs a `sql` string", "invalid_argument"),
    ("timeout", "timeout"),
)


def rejection_category(message: str) -> str:
    for fragment, category in _GATE_CATEGORIES:
        if fragment in message:
            return category
    return "execution_error"


def assert_rejection(expect: dict[str, Any], message: str) -> None:
    category = rejection_category(message)
    assert category in expect["categories"], f"category {category!r} from {message!r}"
    folded = message.casefold()
    for fragment in expect.get("mentions", []):
        assert fragment.casefold() in folded, f"{fragment!r} not named in {message!r}"
    for fragment in expect.get("not_mentions", []):
        assert fragment.casefold() not in folded, f"{fragment!r} leaked in {message!r}"


def _as_double(value: object) -> float:
    return float(cast(str | float | int, value))


def _approx(expected: dict[str, Any], actual: float) -> bool:
    target = float(expected["approx"])
    close = math.isclose(actual, target, rel_tol=expected["rel"], abs_tol=expected["abs"])
    if expected.get("exact") is False and actual == target:
        return False
    return close


def match_cell(expected: Any, text: str) -> bool:
    if expected is None:
        return text == "NULL"
    if isinstance(expected, bool):
        raise TypeError("use {'bool': ...} for boolean matchers")
    if isinstance(expected, str):
        return text == expected
    if isinstance(expected, int):
        return re.fullmatch(r"-?[0-9]+", text) is not None and int(text) == expected
    if "int" in expected:
        return re.fullmatch(r"-?[0-9]+", text) is not None and int(text) == int(expected["int"])
    if "bool" in expected:
        return text.casefold() == str(expected["bool"]).casefold()
    try:
        actual = float(text)
    except ValueError:
        return False
    if "double" in expected:
        return actual == _as_double(expected["double"]) or (
            math.isnan(actual) and math.isnan(_as_double(expected["double"]))
        )
    return _approx(expected, actual)


def match_value(expected: Any, value: object) -> bool:
    if expected is None:
        return value is None
    if isinstance(expected, str):
        return isinstance(value, str) and value == expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    if "int" in expected:
        return (
            isinstance(value, int) and not isinstance(value, bool) and value == int(expected["int"])
        )
    if "bool" in expected:
        return isinstance(value, bool) and value is expected["bool"]
    if isinstance(value, Decimal):
        value = float(value)
    if not isinstance(value, float):
        return False
    if "double" in expected:
        return value == _as_double(expected["double"])
    return _approx(expected, value)


@dataclass(slots=True)
class QueryView:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    shown: int | None = None
    more_rows: bool = False
    rows_omitted: int = 0
    cells_truncated: int = 0
    headers_truncated: int = 0
    table_omitted: bool = False
    fallback: bool = False


def _split_row(line: str) -> list[str]:
    assert line.startswith("| ") and line.endswith(" |"), line
    return line[2:-2].split(" | ")


def _count(pattern: str, text: str) -> int:
    found = re.search(pattern, text)
    return int(found.group(1)) if found else 0


def parse_query_output(text: str) -> QueryView:
    view = QueryView()
    lines = text.split("\n")
    table = [line for line in lines if line.startswith("| ")]
    if table:
        view.columns = _split_row(table[0])
        view.rows = [_split_row(line) for line in table[2:]]
    notice = lines[-1]
    shown = re.search(r"([0-9]+) row\(s\) shown\.", notice)
    view.shown = int(shown.group(1)) if shown else None
    view.more_rows = "Additional rows exist beyond max_rows" in notice
    view.rows_omitted = _count(r"([0-9]+) row\(s\) omitted to stay under the byte cap", notice)
    view.cells_truncated = _count(r"([0-9]+) cell\(s\) truncated", notice)
    view.headers_truncated = _count(r"([0-9]+) column header\(s\) truncated", notice)
    view.table_omitted = "The table was omitted" in notice
    view.fallback = "Output truncated to stay under the configured byte cap" in notice
    return view


def assert_query_view(expect: dict[str, Any], view: QueryView) -> None:
    if "columns" in expect:
        assert view.columns == expect["columns"]
    if "rows" in expect:
        assert len(view.rows) == len(expect["rows"]), view.rows
        for row, expected_row in zip(view.rows, expect["rows"], strict=True):
            assert len(row) == len(expected_row), row
            for cell, matcher in zip(row, expected_row, strict=True):
                assert match_cell(matcher, cell), f"cell {cell!r} does not match {matcher!r}"
    if "row_count" in expect:
        assert len(view.rows) == expect["row_count"]
    for key in ("shown", "more_rows", "rows_omitted", "cells_truncated", "headers_truncated"):
        if key in expect:
            assert getattr(view, key) == expect[key], f"{key}: {getattr(view, key)}"
    if "accounted_rows" in expect:
        assert (view.shown or 0) + view.rows_omitted == expect["accounted_rows"]
    if "rows_omitted_min" in expect:
        assert view.rows_omitted >= expect["rows_omitted_min"]
    if "table_omitted" in expect:
        assert view.table_omitted == expect["table_omitted"]
    if expect.get("headers_truncated_or_table_omitted"):
        assert view.headers_truncated > 0 or view.table_omitted


type AstPath = list[str | int]


def _resolve(document: Any, path: AstPath) -> Any:
    node = document
    for step in path:
        node = node[step]
    return node


def _find(node: Any, wanted: str) -> dict[str, Any]:
    if isinstance(node, dict):
        if node.get("type") == wanted:
            return node
        children: list[Any] = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        children = []
    for child in children:
        try:
            return _find(child, wanted)
        except LookupError:
            continue
    raise LookupError(wanted)


def _substitute(template: Any, node: Any) -> Any:
    if template == "$node":
        return copy.deepcopy(node)
    if isinstance(template, dict):
        return {key: _substitute(value, node) for key, value in template.items()}
    if isinstance(template, list):
        return [_substitute(value, node) for value in template]
    return template


def apply_mutations(
    document: dict[str, Any],
    mutations: list[dict[str, Any]],
    serialize: Any,
) -> dict[str, Any]:
    mutated: dict[str, Any] = json.loads(json.dumps(document))
    for mutation in mutations:
        op = mutation["op"]
        if op == "update":
            scope = _resolve(mutated, mutation.get("within", []))
            _find(scope, mutation["find"]).update(mutation["set"])
        elif op == "set":
            *parent, last = mutation["path"]
            _resolve(mutated, parent)[last] = mutation["value"]
        elif op == "delete":
            *parent, last = mutation["path"]
            del _resolve(mutated, parent)[last]
        elif op == "rename_first":
            target = _resolve(mutated, mutation["path"])
            key = next(k for k in mutation["keys"] if k in target)
            target[mutation["to"]] = target.pop(key)
        elif op == "set_serialized":
            source = serialize(mutation["sql"])["statements"][0]["node"]
            *parent, last = mutation["path"]
            _resolve(mutated, parent)[last] = _substitute(mutation["value"], source)
        else:
            raise ValueError(f"unknown mutation {op!r}")
    return mutated
