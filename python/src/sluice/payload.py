"""Eligibility, channel selection, and previews (spec 5.1).

Pure module: no DuckDB imports, no IO.
"""

import json
from typing import Any

from mcp import types

from sluice.models import Passthrough, PayloadChannel, SelectedPayload

TEXT_KIND = "text"
MAX_CONTENT_KINDS = 32
MAX_CONTENT_KIND_CHARS = 64


def content_kinds(result: types.CallToolResult) -> list[str]:
    """Return a bounded diagnostic summary, never one entry per untrusted block."""
    kinds = [
        str(getattr(block, "type", "unknown"))[:MAX_CONTENT_KIND_CHARS]
        for block in result.content[:MAX_CONTENT_KINDS]
    ]
    if len(result.content) > MAX_CONTENT_KINDS:
        kinds.append("truncated")
    return kinds


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


def metadata_only(*, byte_size: int = 0) -> SelectedPayload:
    """Describe a passthrough result without retaining its textual payload.

    Error, binary, and oversize results are returned verbatim, but their
    envelope record must not become a second unbounded payload cache.
    """
    # Exact wire sizing requires serializing the complete SDK model. Doing that
    # for an unbounded binary, error, or oversize result would create the very
    # second payload copy this metadata-only path exists to avoid. NULL records
    # that the wire size was deliberately not measured.
    return SelectedPayload(channel=PayloadChannel.NONE, byte_size=byte_size, wire_bytes=None)


def classify_passthrough(
    result: types.CallToolResult, max_payload_bytes: int
) -> tuple[Passthrough | None, int | None]:
    """Return the passthrough reason and a safely measured candidate size."""
    if result.is_error:
        return Passthrough.ERROR, None
    if any(getattr(block, "type", "unknown") != TEXT_KIND for block in result.content):
        return Passthrough.NON_TEXT, None
    size = candidate_size(result)
    return (Passthrough.OVERSIZE, size) if size > max_payload_bytes else (None, size)


def concatenated_text(result: types.CallToolResult) -> str:
    return "".join(block.text for block in text_blocks(result))


def wire_bytes(result: types.CallToolResult) -> int:
    return len(result.model_dump_json().encode("utf-8"))


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def candidate_size(result: types.CallToolResult) -> int:
    """Bytes Sluice would process across every retained input channel.

    `structuredContent` wins for table selection, but text is still retained in
    the envelope and compared for channel conflicts. Measuring only the winner
    let a tiny structured object smuggle an arbitrarily large text channel past
    the materialization ceiling. The SDK has already decoded structured content
    before this function runs, so the limit bounds Sluice's subsequent work,
    not memory already spent by the SDK (spec 5.1 step 2).
    """
    structured = (
        _serialized_size(result.structured_content) if result.structured_content is not None else 0
    )
    text = sum(len(block.text.encode("utf-8")) for block in text_blocks(result))
    return structured + text


def passthrough_reason(result: types.CallToolResult, max_payload_bytes: int) -> Passthrough | None:
    """Why this result must be returned unmodified, or None."""
    return classify_passthrough(result, max_payload_bytes)[0]


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


def bounded_preview_rows(rows: list[Any], count: int, limit: int) -> list[Any]:
    """Keep only raw source rows needed for a bounded handle preview."""
    selected: list[Any] = []
    used = 0
    for row in rows[:count]:
        encoded = json.dumps(row, default=str).encode("utf-8")
        separator = 1 if selected else 0
        if used + separator + len(encoded) > limit:
            break
        selected.append(row)
        used += separator + len(encoded)
    return selected
