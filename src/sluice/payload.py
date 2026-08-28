"""Eligibility, channel selection, and previews (spec 5.1).

Pure module: no DuckDB imports, no IO.
"""

import json
from typing import Any

from mcp import types

from sluice.models import Passthrough, PayloadChannel, SelectedPayload

TEXT_KIND = "text"


def content_kinds(result: types.CallToolResult) -> list[str]:
    return [getattr(block, "type", "unknown") for block in result.content]


def text_blocks(result: types.CallToolResult) -> list[types.TextContent]:
    return [b for b in result.content if isinstance(b, types.TextContent)]


def describe_blocks(result: types.CallToolResult) -> list[dict[str, Any]]:
    """An ordered, lossless-enough record of the content blocks.

    `result_text` alone is lossy: two text blocks concatenate irreversibly, and
    the recovery path in spec 6.4 has to be able to return what the server
    actually sent.
    """
    described: list[dict[str, Any]] = []
    for block in result.content:
        kind = getattr(block, "type", "unknown")
        if isinstance(block, types.TextContent):
            described.append({"type": kind, "text": block.text})
        else:
            described.append({"type": kind})
    return described


def concatenated_text(result: types.CallToolResult) -> str:
    return "".join(block.text for block in text_blocks(result))


def wire_bytes(result: types.CallToolResult) -> int:
    return len(result.model_dump_json().encode("utf-8"))


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def candidate_size(result: types.CallToolResult) -> int:
    """Size of the payload that would be selected, measured before parsing.

    For a `structuredContent` payload the SDK has already decoded it before
    Sluice can measure anything, so this bounds what Sluice does next rather
    than what already happened (spec 5.1 step 2).
    """
    if result.structured_content is not None:
        return _serialized_size(result.structured_content)
    return len(concatenated_text(result).encode("utf-8"))


def passthrough_reason(result: types.CallToolResult, max_payload_bytes: int) -> Passthrough | None:
    """Why this result must be returned unmodified, or None."""
    if result.is_error:
        return Passthrough.ERROR
    if any(kind != TEXT_KIND for kind in content_kinds(result)):
        return Passthrough.NON_TEXT
    if candidate_size(result) > max_payload_bytes:
        return Passthrough.OVERSIZE
    return None


def _parse(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except json.JSONDecodeError, ValueError:
        return False, None


def _text_payload(result: types.CallToolResult) -> tuple[bool, Any]:
    """Single parsing block first, then the concatenation.

    Order matters: two independently valid JSON blocks concatenate into invalid
    JSON, and the single-block case is far more common than a genuine multi-block
    document.
    """
    blocks = text_blocks(result)
    parsed = [(_parse(block.text)) for block in blocks]
    successes = [value for ok, value in parsed if ok]
    if len(successes) == 1:
        return True, successes[0]
    return _parse(concatenated_text(result))


def select(result: types.CallToolResult) -> SelectedPayload:
    """Choose the payload channel (spec 5.1 step 3) and detect disagreement."""
    text = concatenated_text(result)
    blocks = describe_blocks(result)
    wire = wire_bytes(result)
    structured = result.structured_content

    text_ok, text_value = _text_payload(result)

    if structured is not None:
        # A tool may put its data here and a prose summary in `content`.
        # Materializing the text in that case flattens the summary and discards
        # the data, which was a blind spot in revision 1 of the spec.
        conflict = text_ok and text_value != structured
        return SelectedPayload(
            channel=PayloadChannel.STRUCTURED,
            value=structured,
            text=text,
            blocks=blocks,
            structured=structured,
            byte_size=_serialized_size(structured),
            wire_bytes=wire,
            conflict=conflict,
        )

    if text_ok:
        return SelectedPayload(
            channel=PayloadChannel.TEXT,
            value=text_value,
            text=text,
            blocks=blocks,
            byte_size=len(text.encode("utf-8")),
            wire_bytes=wire,
        )

    return SelectedPayload(
        channel=PayloadChannel.NONE,
        value=None,
        text=text,
        blocks=blocks,
        byte_size=len(text.encode("utf-8")),
        wire_bytes=wire,
    )


def truncate_to_bytes(text: str, limit: int) -> tuple[str, bool]:
    """Cut to a byte budget on a character boundary.

    Slicing UTF-8 at a byte offset produces invalid text and would corrupt the
    handle the agent depends on.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    cut = encoded[:limit]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


def render_preview(payload: SelectedPayload, limit: int) -> tuple[str, bool]:
    """Return `(preview, complete)`.

    FR-14: a payload smaller than the budget is reproduced in full, and the
    handle says so, so the agent never has to make a `query` round trip for
    something it could simply have been handed.
    """
    if payload.channel is PayloadChannel.NONE:
        source = payload.text or ""
    else:
        source = json.dumps(payload.value, default=str)
    truncated, was_cut = truncate_to_bytes(source, limit)
    return truncated, not was_cut


def render_row_preview(rows: list[Any], count: int, limit: int) -> tuple[str, int]:
    """First `count` rows as JSON lines, bounded by `limit` bytes."""
    shown = rows[:count]
    text = "\n".join(json.dumps(row, default=str) for row in shown)
    truncated, _ = truncate_to_bytes(text, limit)
    return truncated, len(shown)
