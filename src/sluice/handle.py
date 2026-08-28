"""Rendering the handle the agent receives (spec 4)."""

import json
from typing import Any

from mcp import types

from sluice.models import Handle, TableRef
from sluice.store import ENVELOPE_TABLE

QUERY_HINT = "Run SQL over this with the `query` tool."


def _format_columns(table: TableRef) -> list[str]:
    parts: list[str] = []
    for column in table.columns:
        marker = "*" if column.type == "JSON" else ""
        suffix = "" if column.exact else " (inexact)"
        parts.append(f"{column.name} {column.type}{marker}{suffix}")
    return parts


def render_text(handle: Handle) -> str:
    """The content block. This is the only channel guaranteed to reach the model."""
    lines = [
        f"sluice: result recorded.  channel={handle.channel}  scope={handle.scope_id}",
    ]
    if handle.conflict:
        lines.append(
            "warning: structuredContent and the text content both parsed and disagree. "
            "The structured channel was used; the text channel is in result_text."
        )
    for table in handle.tables:
        lines.append(f"table: {table.name}   rows={table.row_count}   from={table.source_path}")
        columns = _format_columns(table)
        if columns:
            lines.append("columns: " + ", ".join(columns))
        if any(column.type == "JSON" for column in table.columns):
            lines.append("         (* JSON: use json_extract and cast before arithmetic)")
        renamed = {c.renamed_from: c.name for c in table.columns if c.renamed_from}
        if renamed:
            lines.append("renamed: " + ", ".join(f"{k} -> {v}" for k, v in renamed.items()))
    if not handle.tables:
        reason = handle.flat_reason or "no tabular rows found"
        lines.append(f"no table: {reason}")
    lines.append(f"envelope: {ENVELOPE_TABLE} WHERE call_id = '{handle.call_id}'")
    if handle.preview_complete:
        lines.append(f"preview (complete, {handle.byte_size} B):")
    else:
        lines.append(f"preview (truncated, of {handle.byte_size} B):")
    lines.append(_indent(handle.preview))
    if handle.query_available:
        lines.append(QUERY_HINT)
    return "\n".join(lines)


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines() or [""])


def render_structured(handle: Handle) -> dict[str, Any]:
    """Mirrors the content block for clients that read structured output."""
    return {
        "call_id": handle.call_id,
        "scope_id": handle.scope_id,
        "envelope_table": ENVELOPE_TABLE,
        "source_channel": str(handle.channel),
        "channel_conflict": handle.conflict,
        "tables": [
            {
                "name": table.name,
                "source_path": table.source_path,
                "row_count": table.row_count,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "exact": column.exact,
                        "renamed_from": column.renamed_from,
                    }
                    for column in table.columns
                ],
            }
            for table in handle.tables
        ],
        "flat_reason": handle.flat_reason,
        "byte_size": handle.byte_size,
        "preview_complete": handle.preview_complete,
        "preview": handle.preview,
    }


def to_result(handle: Handle) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=render_text(handle))],
        structured_content=render_structured(handle),
    )


def size_note(byte_size: int, limit: int) -> str:
    return (
        f"sluice: result passed through unmodified. It is {byte_size} bytes, over the "
        f"{limit}-byte materialization ceiling, so it was not parsed or stored."
    )


def preview_json(value: object, limit: int) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit]
