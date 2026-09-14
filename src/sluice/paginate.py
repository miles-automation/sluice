import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import anyio
from mcp import types

from sluice import payload as payload_select
from sluice.config import PaginationConfig
from sluice.errors import DownstreamError

type PageCall = Callable[
    [dict[str, Any]], Awaitable[types.CallToolResult | types.InputRequiredResult]
]

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
DETAIL_MAX = 200


class StopReason(StrEnum):
    COMPLETE = "complete"
    MAX_PAGES = "max_pages"
    MAX_BYTES = "max_bytes"
    MAX_SECONDS = "max_seconds"
    PAGE_ERROR = "page_error"
    PAGE_FAILED = "page_failed"
    INTERACTIVE = "interactive"
    MALFORMED_PAGE = "malformed_page"
    MISSING_NEXT_OFFSET = "missing_next_offset"
    NOT_ADVANCING = "offset_not_advancing"


class PaginationArgumentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    status: str
    reason: StopReason
    detail: str | None
    pages: int
    bytes: int
    seconds: float
    resume_offset: int | None
    rows: list[Any]
    first_page_error: types.CallToolResult | None


@dataclass(frozen=True, slots=True)
class _Page:
    items: list[Any]
    has_more: bool
    next_offset: object


def split_arguments(
    config: PaginationConfig, arguments: dict[str, Any] | None
) -> tuple[dict[str, Any], int]:
    filters = dict(arguments or {})
    if config.limit_arg in filters:
        raise PaginationArgumentError(
            f"`{config.limit_arg}` is managed by Sluice for this operation "
            f"(page_size={config.page_size}); pass filters only"
        )
    offset = filters.pop(config.offset_arg, 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise PaginationArgumentError(
            f"`{config.offset_arg}` must be a non-negative integer to resume from, got {offset!r}"
        )
    return filters, offset


def _parse_page(config: PaginationConfig, result: types.CallToolResult) -> _Page | str:
    value = payload_select.select(result).value
    if not isinstance(value, dict):
        return "page is not a JSON object"
    items = value.get(config.items)
    if not isinstance(items, list):
        return f"`{config.items}` is missing or not an array"
    has_more = value.get(config.has_more, False)
    if not isinstance(has_more, bool):
        return f"`{config.has_more}` is not a boolean"
    return _Page(items=items, has_more=has_more, next_offset=value.get(config.next_offset))


def _bounded(text: str) -> str:
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX] + "..."


async def fetch_all(
    call: PageCall, config: PaginationConfig, arguments: dict[str, Any] | None
) -> FetchOutcome:
    filters, offset = split_arguments(config, arguments)
    rows: list[Any] = []
    seen: set[int] = set()
    pages = 0
    total_bytes = 0
    reason = StopReason.COMPLETE
    detail: str | None = None
    resume: int | None = None
    first_page_error: types.CallToolResult | None = None
    started = time.monotonic()

    with anyio.move_on_after(config.max_seconds) as deadline:
        while True:
            if pages >= config.max_pages:
                reason, detail, resume = StopReason.MAX_PAGES, f"{pages} pages fetched", offset
                break
            page_args = {**filters, config.limit_arg: config.page_size, config.offset_arg: offset}
            try:
                result = await call(page_args)
            except DownstreamError as exc:
                reason, detail, resume = StopReason.PAGE_FAILED, _bounded(str(exc)), offset
                break
            if isinstance(result, types.InputRequiredResult):
                reason = StopReason.INTERACTIVE
                detail = "the tool requested input, which cannot be answered while paginating"
                resume = offset
                break
            if result.is_error:
                reason = StopReason.PAGE_ERROR
                detail = _bounded(payload_select.concatenated_text(result)) or "downstream isError"
                resume = offset
                if pages == 0:
                    first_page_error = result
                break
            parsed = _parse_page(config, result)
            if isinstance(parsed, str):
                reason, detail, resume = StopReason.MALFORMED_PAGE, parsed, offset
                break
            pages += 1
            total_bytes += payload_select.candidate_size(result)
            rows.extend(parsed.items)
            seen.add(offset)
            if not parsed.has_more:
                break
            next_offset = parsed.next_offset
            if isinstance(next_offset, bool) or not isinstance(next_offset, int):
                reason = StopReason.MISSING_NEXT_OFFSET
                detail = (
                    f"`{config.has_more}` is true but `{config.next_offset}` is {next_offset!r}"
                )
                break
            if next_offset <= offset or next_offset in seen or not parsed.items:
                reason = StopReason.NOT_ADVANCING
                detail = f"offset {offset} -> {next_offset} with {len(parsed.items)} items"
                break
            offset = next_offset
            if total_bytes >= config.max_bytes:
                reason = StopReason.MAX_BYTES
                detail = f"{total_bytes} bytes fetched, ceiling {config.max_bytes}"
                resume = offset
                break

    if deadline.cancelled_caught:
        reason = StopReason.MAX_SECONDS
        detail = f"{config.max_seconds} s elapsed"
        resume = offset
    status = STATUS_COMPLETE if reason is StopReason.COMPLETE else STATUS_PARTIAL
    return FetchOutcome(
        status=status,
        reason=reason,
        detail=detail,
        pages=pages,
        bytes=total_bytes,
        seconds=time.monotonic() - started,
        resume_offset=resume,
        rows=rows,
        first_page_error=first_page_error,
    )
