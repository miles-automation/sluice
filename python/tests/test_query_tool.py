"""The `query` tool through the whole product path."""

import json
import re
import statistics
from collections import Counter

import pytest
from mcp import Client, types

from sluice import naming
from tests.fake_server import rows_payload

pytestmark = pytest.mark.anyio


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


async def _materialize(
    client: Client, tool: str, arguments: dict[str, object]
) -> dict[str, object]:
    result = await client.call_tool(naming.mounted_name("fake", tool), arguments)
    assert result.structured_content is not None
    return dict(result.structured_content)


async def test_query_is_mounted_and_marked_read_only(sluice_client: Client) -> None:
    listing = await sluice_client.list_tools()
    tool = next(t for t in listing.tools if t.name == "query")
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.input_schema["required"] == ["sql"]


async def test_the_handle_points_at_a_table_the_query_can_reach(
    sluice_client: Client,
) -> None:
    handle = await _materialize(sluice_client, "rows", {"n": 400})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool("query", {"sql": f'SELECT count(*) FROM "{table}"'})
    assert result.is_error is not True
    assert "| 400 |" in _text(result)


async def test_the_median_a_model_would_get_wrong(sluice_client: Client) -> None:
    """The demo, mechanically: 400 rows the agent never saw, aggregated exactly."""
    handle = await _materialize(sluice_client, "rows", {"n": 400})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool(
        "query", {"sql": f'SELECT median(score) AS m FROM "{table}"'}
    )
    expected = statistics.median([row["score"] for row in rows_payload(400)])
    assert f"| {expected} |" in _text(result)


async def test_group_by_matches_the_source(sluice_client: Client) -> None:
    handle = await _materialize(sluice_client, "rows", {"n": 400})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool(
        "query", {"sql": f'SELECT tag, count(*) AS n FROM "{table}" GROUP BY tag ORDER BY tag'}
    )
    text = _text(result)
    for tag, count in sorted(Counter(row["tag"] for row in rows_payload(400)).items()):
        assert f"| {tag} | {count} |" in text


async def test_the_envelope_view_reaches_the_untouched_payload(
    sluice_client: Client,
) -> None:
    """Spec 6.4: recovery goes through the scope view, not a fetch tool."""
    handle = await _materialize(sluice_client, "structured_only", {})
    view = handle["envelope_table"]
    call_id = handle["call_id"]
    result = await sluice_client.call_tool(
        "query",
        {"sql": f"SELECT result_structured FROM \"{view}\" WHERE call_id = '{call_id}'"},
    )
    assert result.is_error is not True
    assert "items" in _text(result)


async def test_the_physical_envelope_is_not_reachable(sluice_client: Client) -> None:
    """It holds `flat_tables` for every scope, so one SELECT would hand any
    conversation every other one's table names."""
    await _materialize(sluice_client, "rows", {"n": 5})
    result = await sluice_client.call_tool("query", {"sql": "SELECT * FROM sluice_calls"})
    assert result.is_error is True
    assert "unknown table" in _text(result)


async def test_another_scopes_tables_are_not_reachable_by_name(
    sluice_client: Client,
) -> None:
    """Two calls without a client conversation id get different scopes. Knowing
    one table name must not imply reaching the other's envelope view."""
    first = await _materialize(sluice_client, "rows", {"n": 5})
    second = await _materialize(sluice_client, "rows", {"n": 6})
    assert first["scope_id"] != second["scope_id"]
    rows = await sluice_client.call_tool(
        "query",
        {"sql": f'SELECT count(*) FROM "{second["envelope_table"]}"'},
    )
    # The view exists and is reachable by name, but it is filtered to its scope,
    # so it cannot show the other call.
    assert "| 1 |" in _text(rows)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_tables()",
        "SHOW TABLES",
        "PRAGMA show_tables",
        "DROP TABLE sluice_calls",
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT unnest(range(1000000000))",
        "SELECT write_log('agent-controlled')",
        "SELECT lpad('x', 1000000000, 'x')",
        "SELECT list_resize([1], 1000000000)",
        "SELECT bitstring('1', 1000000000)",
        "SELECT 1; DROP TABLE sluice_calls",
    ],
)
async def test_rejections_are_errors_not_empty_successes(sluice_client: Client, sql: str) -> None:
    """An empty result set would read as "no matches" and be believed."""
    result = await sluice_client.call_tool("query", {"sql": sql})
    assert result.is_error is True
    text = _text(result)
    assert "row(s) shown" not in text
    assert re.search(r"^\|", text, re.MULTILINE) is None


@pytest.mark.parametrize("arguments", [{}, {"sql": ""}, {"sql": "   "}, {"sql": 5}])
async def test_bad_arguments_are_errors(
    sluice_client: Client, arguments: dict[str, object]
) -> None:
    result = await sluice_client.call_tool("query", arguments)
    assert result.is_error is True
    assert "sql" in _text(result)


async def test_max_rows_must_be_an_integer(sluice_client: Client) -> None:
    result = await sluice_client.call_tool("query", {"sql": "SELECT 1", "max_rows": True})
    assert result.is_error is True
    assert "max_rows" in _text(result)


async def test_json_columns_are_usable_from_the_handle(sluice_client: Client) -> None:
    handle = await _materialize(sluice_client, "nested", {})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool(
        "query",
        {"sql": f"SELECT sum(CAST(json_extract(meta, '$.k') AS BIGINT)) AS s FROM \"{table}\""},
    )
    assert "| 10 |" in _text(result)


async def test_a_large_result_never_returns_the_whole_payload(
    sluice_client: Client,
) -> None:
    """The context saving, asserted rather than assumed."""
    handle = await _materialize(sluice_client, "rows", {"n": 400})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool("query", {"sql": f'SELECT * FROM "{table}"'})
    text = _text(result)
    assert "Additional rows exist" in text
    assert "row-0399" not in text
    assert len(text) < len(json.dumps(rows_payload(400)))


async def test_a_nested_cte_cannot_shadow_its_way_to_the_envelope(
    sluice_client: Client,
) -> None:
    """Regression for a complete gate bypass.

    Collecting CTE names globally rather than by lexical scope meant a CTE
    defined inside a subquery whitelisted that name for the whole statement.
    This exact query returned every scope's `flat_tables` through the real
    Sluice path.
    """
    await _materialize(sluice_client, "rows", {"n": 5})
    result = await sluice_client.call_tool(
        "query",
        {
            "sql": (
                "SELECT scope_id, flat_tables FROM sluice_calls s "
                "WHERE EXISTS (WITH sluice_calls AS (SELECT 1) SELECT * FROM sluice_calls)"
            )
        },
    )
    assert result.is_error is True
    assert "unknown table" in _text(result)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master WHERE EXISTS "
        "(WITH sqlite_master AS (SELECT 1) SELECT * FROM sqlite_master)",
        "SELECT * FROM duckdb_tables() WHERE EXISTS "
        "(WITH duckdb_tables AS (SELECT 1) SELECT * FROM duckdb_tables)",
        "WITH sluice_calls AS (SELECT * FROM sluice_calls) SELECT * FROM sluice_calls",
        "WITH x AS (SELECT * FROM sluice_calls), sluice_calls AS (SELECT 1) SELECT * FROM x",
        "WITH x AS (SELECT 1) SELECT * FROM (WITH y AS (SELECT 1) SELECT 1) t, sluice_calls",
    ],
)
async def test_shadowing_variants_are_all_rejected(sluice_client: Client, sql: str) -> None:
    result = await sluice_client.call_tool("query", {"sql": sql})
    assert result.is_error is True


async def test_legitimate_ctes_still_work_through_the_product_path(
    sluice_client: Client,
) -> None:
    """The scoping fix must not cost the agent ordinary CTEs."""
    handle = await _materialize(sluice_client, "rows", {"n": 30})
    table = handle["tables"][0]["name"]  # type: ignore[index]
    result = await sluice_client.call_tool(
        "query",
        {
            "sql": (
                f'WITH high AS (SELECT score FROM "{table}" WHERE score > 0) '
                "SELECT count(*) AS n FROM high"
            )
        },
    )
    assert result.is_error is not True
    assert "| 3" in _text(result) or "| 2" in _text(result)


async def test_recursive_cte_still_works_through_the_product_path(
    sluice_client: Client,
) -> None:
    result = await sluice_client.call_tool(
        "query",
        {
            "sql": (
                "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL "
                "SELECT n + 1 FROM nums WHERE n < 3) SELECT * FROM nums"
            )
        },
    )
    assert result.is_error is not True
    assert "| 1 |" in _text(result)
    assert "| 2 |" in _text(result)
    assert "| 3 |" in _text(result)
