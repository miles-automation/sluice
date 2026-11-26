import copy
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

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


def _marks(case: Case) -> list[Any]:
    only = case.get("only")
    if only is None or only["version"] == "python":
        return []
    return [pytest.mark.skip(reason=f"one-sided, {only['version']} only: {only['reason']}")]


def params(kind: str) -> list[Any]:
    return [
        pytest.param({**fixture.defaults, **case}, id=case["id"], marks=_marks(case))
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


NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
INTEGER = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _number(text: str) -> float | None:
    return float(text) if NUMBER.fullmatch(text) else None


def _expected_double(expected: dict[str, Any]) -> float:
    value = expected["double"]
    if isinstance(value, str):
        parsed = _number(value)
        assert parsed is not None, f"not a numeric literal: {value!r}"
        return parsed
    return float(value)


def _approx(expected: dict[str, Any], actual: float) -> bool:
    target = float(expected["approx"])
    if expected.get("exact") is False and actual == target:
        return False
    return math.isclose(actual, target, rel_tol=expected["rel"], abs_tol=expected["abs"])


def match_cell(expected: Any, text: str) -> bool:
    if expected is None:
        return text == "NULL"
    if isinstance(expected, bool):
        raise TypeError("use {'bool': ...} for boolean matchers")
    if isinstance(expected, str):
        return text == expected
    if isinstance(expected, int):
        return INTEGER.fullmatch(text) is not None and int(text) == expected
    if "int" in expected:
        return INTEGER.fullmatch(text) is not None and int(text) == int(expected["int"])
    if "bool" in expected:
        return (
            text in ("true", "false", "True", "False")
            and text.lower() == str(expected["bool"]).lower()
        )
    if "decimal" in expected:
        return DECIMAL.fullmatch(text) is not None and text == expected["decimal"]
    actual = _number(text)
    if actual is None:
        return False
    if "double" in expected:
        return actual == _expected_double(expected)
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
    if "decimal" in expected:
        return isinstance(value, Decimal) and str(value) == expected["decimal"]
    if not isinstance(value, float):
        return False
    if "double" in expected:
        return value == _expected_double(expected)
    return _approx(expected, value)


@dataclass(slots=True)
class QueryView:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    shown: int = 0
    more_rows: bool = False
    rows_omitted: int = 0
    cells_truncated: int = 0
    headers_truncated: int = 0
    table_omitted: bool = False


NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shown", re.compile(r"([0-9]+) row\(s\) shown\.")),
    (
        "more_rows",
        re.compile(r"Additional rows exist beyond max_rows; the exact number is not known\."),
    ),
    ("rows_omitted", re.compile(r"([0-9]+) row\(s\) omitted to stay under the byte cap\.")),
    ("cells_truncated", re.compile(r"([0-9]+) cell\(s\) truncated, marked with a trailing …\.")),
    (
        "headers_truncated",
        re.compile(r"([0-9]+) column header\(s\) truncated, marked with a trailing …\."),
    ),
    (
        "table_omitted",
        re.compile(r"The table was omitted because its header exceeded the output byte cap\."),
    ),
    ("fallback", re.compile(r"Output truncated to stay under the configured byte cap\.")),
)


def _split_row(line: str) -> list[str]:
    assert line.startswith("| ") and line.endswith(" |"), line
    return line[2:-2].split(" | ")


def parse_notices(line: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    rest = line
    for kind, pattern in NOTICES:
        match = pattern.match(rest)
        if match is None:
            continue
        found.append((kind, int(match.group(1)) if match.groups() else 1))
        rest = rest[match.end() :]
        if rest.startswith(" "):
            rest = rest[1:]
    assert rest == "", f"unrecognized notice text {rest!r} in {line!r}"
    assert found and (found[0][0] == "shown" or found == [("fallback", 1)]), line
    return found


def parse_query_output(text: str) -> QueryView:
    view = QueryView()
    body, separator, notice = text.rpartition("\n\n")
    lines = body.split("\n") if separator else []
    if not separator:
        notice = text
    table = [line for line in lines if line.startswith("| ")]
    assert len(table) == len(lines) or lines == ["(no columns)"], f"unexpected body {body!r}"
    if table:
        assert len(table) >= 2, table
        view.columns = _split_row(table[0])
        view.rows = [_split_row(line) for line in table[2:]]
    for kind, count in parse_notices(notice):
        view.notices.append(kind)
        if kind == "fallback":
            continue
        if kind in ("more_rows", "table_omitted"):
            setattr(view, kind, True)
        else:
            setattr(view, kind, count)
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
    if "notices" in expect:
        assert view.notices == expect["notices"]
    if "accounted_rows" in expect:
        assert view.shown + view.rows_omitted == expect["accounted_rows"]
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
    sql: str,
    mutations: list[dict[str, Any]],
    serialize: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    first, *rest = mutations
    if first["op"] == "replace_serialized":
        mutated = serialize(first["sql"])
    else:
        mutated = serialize(sql)
        rest = mutations
    mutated = copy.deepcopy(mutated)
    for mutation in rest:
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
            raise ValueError(f"mutation {op!r} is unknown or out of place")
    return mutated
