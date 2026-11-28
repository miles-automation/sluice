import ast
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
import duckdb
import pytest
from mcp import types

from sluice import gate, infer, naming, scope, shape
from sluice.config import ConfigError, Limits, PaginationConfig, load_config
from sluice.errors import DownstreamError, FailureClass
from sluice.gate import QueryRejectedError
from sluice.intercept import Interceptor
from sluice.paginate import FetchOutcome, PageCall, PaginationArgumentError, fetch_all
from sluice.proxy import MountedCollection, Proxy, expose_collection
from sluice.query import QueryTool, escape_cell
from sluice.server import _run_collection, _run_query
from sluice.store import Store, scope_view_name
from sluice.supervisor import SupervisedStore
from tests.conformance_support import (
    CONFORMANCE_ROOT,
    FIXTURE_SET,
    LOCKDOWN,
    Case,
    apply_mutations,
    assert_query_view,
    assert_rejection,
    match_value,
    params,
    parse_query_output,
)

KINDS = {
    "extract",
    "project",
    "infer",
    "naming",
    "scope",
    "cell",
    "gate",
    "gate_ast",
    "engine",
    "session",
    "paginate",
    "config",
}


def test_every_fixture_kind_has_a_python_runner() -> None:
    assert {fixture.kind for fixture in FIXTURE_SET} <= KINDS


@pytest.mark.parametrize("case", params("extract"))
def test_extract(case: Case) -> None:
    result = shape.extract(json.loads(case["payload_json"]))
    expect = case["expect"]
    if "row_sets" in expect:
        assert result.reason is None
        assert [{"source_path": rs.source_path, "rows": rs.rows} for rs in result.row_sets] == (
            expect["row_sets"]
        )
        return
    assert result.row_sets == []
    assert result.reason is not None
    assert result.reason.split(":", 1)[0] == expect["reason"]
    for path in expect.get("paths", []):
        assert path in result.reason


@pytest.mark.parametrize("case", params("project"))
def test_project(case: Case) -> None:
    projection = shape.project(json.loads(case["rows_json"]), case["max_columns"])
    expect = case["expect"]
    if "columns" in expect:
        assert projection.columns == expect["columns"]
    if "columns_prefix" in expect:
        assert projection.columns[: len(expect["columns_prefix"])] == expect["columns_prefix"]
    if "column_count" in expect:
        assert len(projection.columns) == expect["column_count"]
    if "renamed" in expect:
        assert projection.renamed == expect["renamed"]
    if "extra_keys" in expect:
        assert projection.extra_keys == expect["extra_keys"]
    if "records" in expect:
        assert projection.records == expect["records"]


@pytest.mark.parametrize("case", params("infer"))
def test_infer(case: Case) -> None:
    column_type, exact = infer.infer_column(json.loads(case["values_json"]))
    assert (str(column_type), exact) == (case["expect"]["type"], case["expect"]["exact"])


def _naming(op: str, args: dict[str, Any]) -> Any:
    if op == "slug":
        return naming.slug(args["value"])
    if op == "tag":
        return naming.tag(args["value"])
    if op == "mounted_name":
        return naming.mounted_name(args["server"], args["tool"])
    if op == "collection_name":
        return naming.collection_name(args["server"], args["tool"])
    if op == "table_name":
        mounted = naming.mounted_name(args["server"], args["tool"])
        return naming.table_name(mounted, args["scope"], args["seq"])
    if op == "quote_ident":
        return naming.quote_ident(args["value"])
    if op == "assert_injective":
        return naming.assert_injective([(pair[0], pair[1]) for pair in args["pairs"]])
    if op == "distinct":
        names = [_naming(item["op"], item["args"]) for item in args["items"]]
        return len(set(names)) == len(names)
    raise AssertionError(op)


@pytest.mark.parametrize("case", params("naming"))
def test_naming(case: Case) -> None:
    value = _naming(case["op"], case["args"])
    expect = case["expect"]
    if "value" in expect:
        assert value == expect["value"]
    if "prefix" in expect:
        assert value.startswith(expect["prefix"])
    if "suffix" in expect:
        assert value.endswith(expect["suffix"])
    if "max_length" in expect:
        assert len(value) <= expect["max_length"]
    if "distinct" in expect:
        assert value is expect["distinct"]
    if "count" in expect:
        assert len(value) == expect["count"]


@pytest.mark.parametrize("case", params("scope"))
def test_scope(case: Case) -> None:
    expect = case["expect"]
    if case["op"] == "from_conversation_id":
        assert scope.from_conversation_id(case["conversation_id"]) == expect["scope"]
        return
    if case["op"] == "mint":
        minted = {scope.mint() for _ in range(expect["distinct_over"])}
        assert len(minted) == expect["distinct_over"]
        assert all(re.fullmatch(expect["pattern"], tag) for tag in minted)
        return
    tag, from_client = scope.derive(case.get("meta"))
    assert from_client is expect["from_client"]
    if "scope" in expect:
        assert tag == expect["scope"]
    if "pattern" in expect:
        assert re.fullmatch(expect["pattern"], tag)
    if "distinct_over" in expect:
        derived = {scope.derive(case.get("meta"))[0] for _ in range(expect["distinct_over"])}
        assert len(derived) == expect["distinct_over"]


@pytest.mark.parametrize("case", params("cell"))
def test_cell(case: Case) -> None:
    text, truncated = escape_cell(case["value"], case["max_bytes"])
    assert (text, truncated) == (case["expect"]["text"], case["expect"]["truncated"])
    if truncated:
        assert len(text.encode("utf-8")) <= case["max_bytes"]


def _gate_connection(case: Case) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    for statement in case.get("setup", []):
        connection.execute(statement)
    for statement in LOCKDOWN:
        connection.execute(statement)
    return connection


def _assert_gate(case: Case, connection: duckdb.DuckDBPyConnection) -> None:
    expect = case["expect"]
    allowed = set(case.get("allowed", []))
    evicted = set(case.get("evicted", []))
    if expect["decision"] == "allow":
        gate.check(case["sql"], connection, allowed, evicted)
        if expect.get("executes"):
            connection.execute(case["sql"]).fetchall()
        return
    with pytest.raises(QueryRejectedError) as caught:
        gate.check(case["sql"], connection, allowed, evicted)
    assert_rejection(expect, str(caught.value))


@pytest.mark.parametrize("case", params("gate"))
def test_gate(case: Case) -> None:
    connection = _gate_connection(case)
    try:
        _assert_gate(case, connection)
    finally:
        connection.close()


@pytest.mark.parametrize("case", params("gate_ast"))
def test_gate_ast(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _gate_connection(case)
    real = gate._serialize

    def mutated(sql: str, con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        return apply_mutations(sql, case["mutations"], lambda other: real(other, connection))

    monkeypatch.setattr(gate, "_serialize", mutated)
    try:
        _assert_gate(case, connection)
    finally:
        connection.close()


@pytest.mark.parametrize("case", params("engine"))
def test_engine(case: Case) -> None:
    connection = duckdb.connect(":memory:")
    try:
        for statement in case.get("setup", []):
            connection.execute(statement)
        if case.get("lockdown"):
            for statement in LOCKDOWN:
                connection.execute(statement)
        for statement in case.get("locked_setup", []):
            connection.execute(statement)
        expect = case["expect"]
        if "error" in expect:
            error_type = (
                duckdb.PermissionException if expect["error"] == "permission" else duckdb.Error
            )
            with pytest.raises(error_type):
                connection.execute(case["sql"]).fetchall()
            return
        rows = connection.execute(case["sql"]).fetchall()
        if expect.get("nonempty"):
            assert rows
        if "rows" in expect:
            assert len(rows) == len(expect["rows"])
            for row, expected_row in zip(rows, expect["rows"], strict=True):
                assert len(row) == len(expected_row)
                for value, matcher in zip(row, expected_row, strict=True):
                    assert match_value(matcher, value), f"{value!r} does not match {matcher!r}"
    finally:
        connection.close()


def _block(block: dict[str, Any]) -> types.ContentBlock:
    if block["type"] == "text":
        return types.TextContent(type="text", text=block["text"])
    return types.ImageContent(type="image", data=block["data"], mime_type=block["mimeType"])


def _result(spec: dict[str, Any]) -> types.CallToolResult:
    structured = spec.get("structured_content_json")
    return types.CallToolResult(
        content=[_block(block) for block in spec["content"]],
        structured_content=json.loads(structured) if structured is not None else None,
        is_error=spec.get("is_error", False),
    )


def _flat_reason_category(reason: object) -> str | None:
    if reason is None:
        return None
    assert isinstance(reason, str)
    return re.split(r"[:;]", reason, maxsplit=1)[0].strip()


def _assert_handle(expect: dict[str, Any], result: types.CallToolResult) -> dict[str, Any]:
    structured = result.structured_content
    assert structured is not None, "expected a handle"
    assert result.is_error is not True
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    text = block.text
    assert str(structured["envelope_table"]).startswith(scope_view_name(structured["scope_id"]))
    assert structured["envelope_table"] in text
    for key in ("source_channel", "channel_conflict", "preview_complete", "scope_id"):
        if key in expect:
            assert structured[key] == expect[key], key
    if "flat_reason" in expect:
        assert _flat_reason_category(structured["flat_reason"]) == expect["flat_reason"]
    if "preview_equals" in expect:
        assert structured["preview"] == expect["preview_equals"]
    for fragment in expect.get("preview_contains", []):
        assert fragment in structured["preview"]
    for fragment in expect.get("preview_excludes", []):
        assert fragment not in structured["preview"]
    for fragment in expect.get("text_contains", []):
        assert fragment in text
    if "text_max_bytes" in expect:
        assert len(text.encode("utf-8")) <= expect["text_max_bytes"]
    if "preview_rows" in expect or "preview_total_rows" in expect:
        found = re.search(r"preview \(first ([0-9]+) of ([0-9]+) rows", text)
        assert found is not None, text
        if "preview_rows" in expect:
            assert int(found.group(1)) == expect["preview_rows"]
        if "preview_total_rows" in expect:
            assert int(found.group(2)) == expect["preview_total_rows"]
    if "tables" in expect:
        tables = structured["tables"]
        assert len(tables) == len(expect["tables"])
        for table, wanted in zip(tables, expect["tables"], strict=True):
            assert table["name"] in text
            if "name_suffix" in wanted:
                assert table["name"].endswith(wanted["name_suffix"]), table["name"]
            assert table["source_path"] == wanted["source_path"]
            assert table["row_count"] == wanted["row_count"]
            columns = [
                {key: value for key, value in column.items() if value is not None}
                for column in table["columns"]
            ]
            if "columns" in wanted:
                assert columns == [_column(column) for column in wanted["columns"]]
            if "column_count" in wanted:
                assert len(columns) == wanted["column_count"]
            for column in wanted.get("columns_include", []):
                assert _column(column) in columns
    return dict(structured)


def _column(column: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in column.items() if value is not None}


def _assert_passthrough(
    expect: dict[str, Any], original: types.CallToolResult, result: types.CallToolResult
) -> None:
    assert result.structured_content == original.structured_content
    assert result.is_error == original.is_error
    assert result.content[: len(original.content)] == original.content
    extra = result.content[len(original.content) :]
    if expect["passthrough"] == "oversize" or expect.get("size_note"):
        assert len(extra) == 1 and isinstance(extra[0], types.TextContent)
    else:
        assert extra == []


class _Session:
    def __init__(self, limits: Limits, store: SupervisedStore) -> None:
        self.limits = limits
        self.store = store
        self.interceptor = Interceptor(store, limits, query_available=True)
        self.query = QueryTool(store, limits)
        self.calls: dict[str, dict[str, Any]] = {}

    def substitute(self, value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\{\{([a-z0-9._-]+?)\.([a-z_]+)(?:\.([0-9]+))?\}\}", self._name, value)
        return value

    def _name(self, match: re.Match[str]) -> str:
        call = self.calls[match.group(1)]
        field_name = match.group(2)
        if field_name == "table":
            return str(call["tables"][int(match.group(3) or 0)]["name"])
        if field_name == "envelope":
            return str(call["envelope"])
        if field_name == "call_id":
            return str(call["call_id"])
        if field_name == "scope":
            return str(call["scope_id"])
        raise AssertionError(field_name)

    async def call(self, step: dict[str, Any]) -> None:
        original = _result(step["result"])
        server = step.get("server", "conformance")
        tool = step.get("tool", "tool")
        result = await self.interceptor.intercept(
            server=server,
            tool=tool,
            mounted=naming.mounted_name(server, tool),
            arguments=step.get("arguments"),
            result=original,
            meta=step.get("meta"),
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        expect = step["expect"]
        if expect["outcome"] == "passthrough":
            _assert_passthrough(expect, original, result)
            derived, from_client = scope.derive(step.get("meta"))
            self.calls[step["call"]] = {
                "tables": [],
                "scope_id": derived if from_client else None,
                "envelope": scope_view_name(derived, self.store.generation)
                if from_client
                else None,
            }
            return
        self.calls[step["call"]] = {
            **_assert_handle(expect, result),
            "envelope": result.structured_content["envelope_table"]
            if result.structured_content
            else None,
        }

    async def run_query(self, step: dict[str, Any]) -> None:
        arguments = {key: self.substitute(value) for key, value in step["query"].items()}
        result = await _run_query(
            self.query, types.CallToolRequestParams(name="query", arguments=arguments)
        )
        block = result.content[0]
        assert isinstance(block, types.TextContent)
        text = block.text
        expect = step["expect"]
        if expect.get("error"):
            assert result.is_error is True, text
            assert "row(s) shown" not in text
            assert re.search(r"^\|", text, re.MULTILINE) is None
            if "categories" in expect:
                assert_rejection(expect, text)
            return
        assert result.is_error is not True, text
        assert len(text.encode("utf-8")) <= self.limits.query_max_bytes
        view = parse_query_output(text)
        assert view.shown == len(view.rows)
        assert_query_view(expect, view)
        for fragment in expect.get("text_excludes", []):
            assert fragment not in text


@pytest.mark.parametrize("case", params("session"))
def test_session(case: Case) -> None:
    limits = Limits(**case.get("limits", {}))

    async def run() -> None:
        with SupervisedStore.open(limits) as store:
            session = _Session(limits, store)
            for step in case["steps"]:
                if "call" in step:
                    await session.call(step)
                else:
                    await session.run_query(step)

    anyio.run(run)


_FAILURES = {
    "tool_error": FailureClass.TOOL_ERROR,
    "output_schema": FailureClass.OUTPUT_SCHEMA,
    "protocol": FailureClass.PROTOCOL,
    "transport": FailureClass.TRANSPORT,
}


@pytest.mark.parametrize("case", params("paginate"))
def test_paginate(case: Case) -> None:
    config = PaginationConfig(tool="t", **case.get("config", {}))
    seen: list[dict[str, Any]] = []

    async def call(arguments: dict[str, Any]) -> types.CallToolResult | types.InputRequiredResult:
        seen.append(dict(arguments))
        response = case["pages"][str(arguments[config.offset_arg])]
        if "failure" in response:
            raise DownstreamError(_FAILURES[response["failure"]], "t", response["message"])
        if response.get("input_required"):
            return types.InputRequiredResult(
                input_requests={
                    "pick": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Continue?",
                            requested_schema={"type": "object", "properties": {}},
                        )
                    )
                },
                request_state="awaiting",
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=response["text"])],
            is_error=response.get("is_error", False),
        )

    expect = case["expect"]

    async def run() -> None:
        if expect.get("argument_error"):
            with pytest.raises(PaginationArgumentError) as caught:
                await fetch_all(call, config, case.get("arguments"))
            for fragment in expect.get("mentions", []):
                assert fragment in str(caught.value)
            assert seen == []
            return
        outcome = await fetch_all(call, config, case.get("arguments"))
        assert outcome.status == expect["status"]
        assert str(outcome.reason) == expect["reason"]
        for key in ("pages", "bytes", "resume_offset", "rows"):
            if key in expect:
                assert getattr(outcome, key) == expect[key], key
        if "calls" in expect:
            assert seen == expect["calls"]
        for fragment in expect.get("detail_contains", []):
            assert outcome.detail is not None and fragment in outcome.detail
        if "first_page_error" in expect:
            assert (outcome.first_page_error is not None) is expect["first_page_error"]

    anyio.run(run)
    if "collection" in case:
        seen.clear()
        anyio.run(_assert_collection, case, config, call)


class _ScriptedProxy:
    def __init__(self, call: PageCall) -> None:
        self._call = call

    async def fetch_collection(
        self, entry: MountedCollection, arguments: dict[str, Any] | None
    ) -> FetchOutcome:
        return await fetch_all(self._call, entry.config, arguments)


async def _assert_collection(case: Case, config: PaginationConfig, call: PageCall) -> None:
    expect = case["collection"]
    tool = types.Tool(
        name="t",
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(read_only_hint=True),
    )
    entry = expose_collection("conformance", tool, config)
    params = types.CallToolRequestParams(name=entry.mounted, arguments=case.get("arguments"))
    limits = Limits()
    with Store.open(limits) as store:
        result = await _run_collection(
            cast(Proxy, _ScriptedProxy(call)),
            Interceptor(store, limits, query_available=True),
            entry,
            params,
            datetime.now(UTC).replace(tzinfo=None),
        )
        tables = store.allowed_objects - {
            name for name in store.allowed_objects if name.startswith("sluice_calls__")
        }
    structured = result.structured_content
    if expect["outcome"] == "error":
        assert result.is_error is True
        assert structured is None or structured.get("tables") == []
        assert tables == set(), "a fetch with no rows must not leave a table"
        texts = [block.text for block in result.content if isinstance(block, types.TextContent)]
        assert texts
        if "keeps_downstream_error" in expect:
            first = case["pages"]["0"]
            kept = bool(texts) and texts[0] == first.get("text") and len(texts) >= 2
            assert kept is expect["keeps_downstream_error"]
        return
    assert result.is_error is not True
    assert structured is not None
    if expect["outcome"] == "envelope_only":
        assert structured["tables"] == []
        assert tables == set()
    else:
        assert len(structured["tables"]) == 1
        assert len(tables) == 1
        if "table_rows" in expect:
            assert structured["tables"][0]["row_count"] == expect["table_rows"]
    pagination = structured["pagination"]
    for key in ("status", "reason", "resume_offset"):
        if key in expect:
            assert pagination[key] == expect[key], key


@pytest.mark.parametrize("case", params("config"))
def test_config(case: Case, tmp_path: Path) -> None:
    if "toml_file" in case:
        path = CONFORMANCE_ROOT.parent / case["toml_file"]
    else:
        path = tmp_path / "sluice.toml"
        if "toml" in case:
            path.write_text(case["toml"], encoding="utf-8")
    environ = case.get("environ", {})
    expect = case["expect"]
    if expect.get("error"):
        with pytest.raises(ConfigError) as caught:
            load_config(path, environ=environ)
        for fragment in expect.get("mentions", []):
            assert fragment in str(caught.value)
        return
    config = load_config(path, environ=environ)
    for key, value in expect.get("server", {}).items():
        assert getattr(config.server, key) == value, key
    for key, value in expect.get("limits", {}).items():
        assert getattr(config.limits, key) == value, key
    if "pagination" in expect:
        assert set(config.pagination) == set(expect["pagination"])
        for tool, fields in expect["pagination"].items():
            for key, value in fields.items():
                assert getattr(config.pagination[tool], key) == value, f"{tool}.{key}"


_ID_PREFIXES = tuple(f"{kind}." for kind in KINDS)


def test_coverage_map_lists_every_python_test() -> None:
    coverage = (CONFORMANCE_ROOT / "COVERAGE.md").read_text(encoding="utf-8")
    listed: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in coverage.splitlines():
        heading = re.fullmatch(r"## tests/(test_[a-z0-9_]+\.py)", line)
        if heading:
            current = listed.setdefault(heading.group(1), set())
            continue
        row = re.match(r"\| `(test_[a-z0-9_]+)` \|", line)
        if row and current is not None:
            current.add(row.group(1))
    tests_dir = Path(__file__).parent
    actual = {
        path.name: {
            node.name
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        }
        for path in tests_dir.glob("test_*.py")
    }
    assert listed == actual
    ids = {case["id"] for fixture in FIXTURE_SET for case in fixture.cases}
    mapped_section, _, unmapped_section = coverage.partition(
        "\n## Fixtures with no prior Python test\n"
    )
    mapped = "\n".join(line for line in mapped_section.splitlines() if re.match(r"\| `test_", line))
    covered: set[str] = set()
    for pattern in re.findall(r"`([a-z0-9_]+\.[a-z0-9.*_-]+)`", mapped):
        if not pattern.startswith(_ID_PREFIXES):
            continue
        matcher = re.compile(re.escape(pattern).replace(r"\*", ".*"))
        matched = {case_id for case_id in ids if matcher.fullmatch(case_id)}
        assert matched, pattern
        covered |= matched
    unmapped = set(re.findall(r"^- `([^`]+)`$", unmapped_section, re.MULTILINE))
    assert unmapped == ids - covered
    for pattern in re.findall(r"`([a-z0-9_]+\.[a-z0-9.*_-]+)`", coverage):
        if pattern.startswith(_ID_PREFIXES):
            matcher = re.compile(re.escape(pattern).replace(r"\*", ".*"))
            assert any(matcher.fullmatch(case_id) for case_id in ids), pattern
