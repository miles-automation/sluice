"""Measure RSS for Sluice's file-free materialization pipeline.

This is intentionally a benchmark rather than a test.  Each scenario runs in
its own child process because ``ru_maxrss`` is a process high-water mark.  The
scenario calls the same ``Interceptor.intercept`` and ``Store`` code used by
the server, with a structured result (the SDK has already decoded this channel
by the time Sluice sees it).

Examples::

    uv run python benchmarks/memory_materialization.py --all
    uv run python benchmarks/memory_materialization.py --shape nested \
        --target-mib 16 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import platform
import resource
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import types

from sluice.config import Limits
from sluice.intercept import Interceptor
from sluice.naming import quote_ident
from sluice.store import Store

SHAPES = ("flat", "nested", "wide", "mixed")
SIZES_MIB = (1, 4, 16, 24, 30)
CONCURRENCIES = (1, 2, 4)
MODES = ("structured", "text", "dual")
# The default matrix covers the configured concurrency and the useful scaling
# points without making a full run needlessly long.  30 MiB and concurrency 4
# remain available as one-off stress scenarios.
MATRIX_SIZES_MIB = (1, 4, 16)
MATRIX_CONCURRENCIES = (1, 2)
BENCHMARK_LIMIT = 128 * 1024 * 1024


def rss_high_water_bytes() -> int:
    """Return ru_maxrss in bytes on macOS and Linux."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if platform.system() == "Darwin" else value * 1024)


def _row(shape: str, index: int, padding: int) -> dict[str, Any]:
    text = f"row-{index:08d}-" + ("x" * padding)
    if shape == "flat":
        return {
            "id": index,
            "score": index / 3,
            "name": text,
            "active": index % 2 == 0,
            "group": f"g-{index % 17}",
        }
    if shape == "nested":
        return {
            "id": index,
            "name": text,
            "metadata": {
                "ordinal": index,
                "labels": [f"label-{index % 11}", "stable"],
            },
            "measurements": [index, index + 1, index + 2],
        }
    if shape == "wide":
        return {
            **{f"field_{column:02d}": index + column for column in range(64)},
            "id": index,
            "padding": text,
        }
    if shape == "mixed":
        row: dict[str, Any] = {
            "id": index,
            "score": index / 7,
            "name": text,
            "active": index % 2 == 0,
            "nested": {"ordinal": index, "ok": True},
        }
        if index % 3 == 0:
            row["optional"] = index
        if index % 5 == 0:
            row["optional_text"] = f"value-{index}"
        return row
    raise ValueError(f"unknown shape: {shape}")


def make_payload(shape: str, target_bytes: int) -> tuple[dict[str, Any], int, int]:
    """Build an object envelope containing a row array near ``target_bytes``."""
    # A row-level estimate avoids repeatedly serializing the whole growing
    # document.  The final size is measured with the exact serializer Sluice
    # uses for structured payload accounting.
    sample = json.dumps(_row(shape, 0, 32)).encode()
    rows_needed = max(1, target_bytes // max(len(sample), 1))
    rows: list[dict[str, Any]] = []
    lengths: list[int] = []
    row_bytes = 0
    # Keep a cheap running estimate while growing the list. Serializing the
    # entire list on every iteration makes a multi-megabyte case quadratic.
    while len(rows) < rows_needed or row_bytes + len(rows) + 12 < target_bytes:
        index = len(rows)
        item = _row(shape, index, 32)
        rows.append(item)
        length = len(json.dumps(item).encode())
        lengths.append(length)
        row_bytes += length
    # The default JSON encoder separates list elements with `, ` and wraps the
    # object in `{"items": [...]}`. Trim using per-row lengths before the one
    # full serialization below; this keeps target sizes close without an O(n²)
    # sizing loop.
    prefix = len(json.dumps({"items": []}).encode())
    while len(rows) > 1 and prefix + row_bytes + 2 * (len(rows) - 1) > target_bytes:
        row_bytes -= lengths.pop()
        rows.pop()
    payload: dict[str, Any] = {"items": rows}
    actual = len(json.dumps(payload).encode())
    return payload, len(rows), actual


async def run_pipeline(payloads: Iterable[dict[str, Any]], concurrency: int, mode: str) -> None:
    """Run the production interception path for all payloads."""
    payload_list = list(payloads)
    limits = Limits(
        max_payload_bytes=BENCHMARK_LIMIT,
        max_concurrent_materializations=concurrency,
    )
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)

        async def one(index: int, payload: dict[str, Any]) -> types.CallToolResult:
            text = json.dumps(payload)
            result = types.CallToolResult(
                content=(
                    [types.TextContent(type="text", text=text)]
                    if mode in ("text", "dual")
                    else [types.TextContent(type="text", text="")]
                ),
                structuredContent=payload if mode in ("structured", "dual") else None,
            )
            return await interceptor.intercept(
                server="benchmark",
                tool=f"rows_{index}",
                mounted=f"benchmark__rows_{index}",
                arguments=None,
                result=result,
                meta=None,
                started_at=datetime.now(UTC).replace(tzinfo=None),
            )

        results = await asyncio.gather(
            *(one(index, payload) for index, payload in enumerate(payload_list))
        )
        if len(results) != len(payload_list):
            raise RuntimeError("not every benchmark payload completed")
        for result in results:
            content = result.structured_content
            if not isinstance(content, dict) or not content.get("tables"):
                raise RuntimeError("interception did not return a materialized table handle")
            for table in content["tables"]:
                name = table.get("name")
                if not isinstance(name, str):
                    raise RuntimeError("materialized handle did not name a table")
                store.connection.execute(f"SELECT count(*) FROM {quote_ident(name)}").fetchone()
        stored_calls = store.connection.execute("SELECT count(*) FROM sluice_calls").fetchone()
        if stored_calls != (len(payload_list),):
            raise RuntimeError(f"expected {len(payload_list)} envelope rows, got {stored_calls}")


async def run_sequential(payload: dict[str, Any], calls: int, mode: str) -> tuple[int, list[int]]:
    """Run calls serially while retaining all materialized tables in one session."""
    limits = Limits(max_payload_bytes=BENCHMARK_LIMIT, max_concurrent_materializations=2)
    with Store.open(limits) as store:
        interceptor = Interceptor(store, limits)
        baseline = rss_high_water_bytes()
        peaks: list[int] = []
        for index in range(calls):
            # Treat every response as a fresh SDK-decoded object, as a real
            # downstream call would. The accumulated tables remain live in the
            # Store for the duration of this long-session scenario.
            call_payload = copy.deepcopy(payload)
            text = json.dumps(call_payload)
            result = types.CallToolResult(
                content=(
                    [types.TextContent(type="text", text=text)]
                    if mode in ("text", "dual")
                    else [types.TextContent(type="text", text="")]
                ),
                structuredContent=call_payload if mode in ("structured", "dual") else None,
            )
            intercepted = await interceptor.intercept(
                server="benchmark",
                tool=f"rows_{index}",
                mounted=f"benchmark__rows_{index}",
                arguments=None,
                result=result,
                meta=None,
                started_at=datetime.now(UTC).replace(tzinfo=None),
            )
            content = intercepted.structured_content
            if not isinstance(content, dict) or not content.get("tables"):
                raise RuntimeError("sequential interception did not materialize a table")
            for table in content["tables"]:
                name = table.get("name")
                if not isinstance(name, str):
                    raise RuntimeError("sequential handle did not name a table")
                store.connection.execute(f"SELECT count(*) FROM {quote_ident(name)}").fetchone()
            peaks.append(rss_high_water_bytes())
        stored_calls = store.connection.execute("SELECT count(*) FROM sluice_calls").fetchone()
        if stored_calls != (calls,):
            raise RuntimeError(f"expected {calls} envelope rows, got {stored_calls}")
    return baseline, peaks


def run_scenario(shape: str, target_mib: int, concurrency: int, mode: str) -> dict[str, Any]:
    target_bytes = target_mib * 1024 * 1024
    payload, rows, payload_bytes = make_payload(shape, target_bytes)
    # Production receives an independently decoded object for each call. Make
    # those copies before the baseline high-water reading so the reported
    # pipeline increment excludes input construction itself.
    payloads = [payload, *(copy.deepcopy(payload) for _ in range(concurrency - 1))]
    baseline = rss_high_water_bytes()
    started = time.perf_counter()
    asyncio.run(run_pipeline(payloads, concurrency, mode))
    elapsed = time.perf_counter() - started
    peak = rss_high_water_bytes()
    result = {
        "shape": shape,
        "mode": mode,
        "target_mib": target_mib,
        "concurrency": concurrency,
        "payload_bytes": payload_bytes,
        "payload_mib": payload_bytes / 1024 / 1024,
        "rows_per_call": rows,
        "baseline_rss_high_water_bytes": baseline,
        "peak_rss_high_water_bytes": peak,
        "pipeline_increment_high_water_bytes": max(0, peak - baseline),
        "rss_multiple_absolute": peak / payload_bytes,
        "rss_multiple_increment": max(0, peak - baseline) / payload_bytes,
        "elapsed_seconds": elapsed,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    print(json.dumps(result, sort_keys=True))
    return result


def run_sequential_scenario(shape: str, target_mib: int, calls: int, mode: str) -> None:
    target_bytes = target_mib * 1024 * 1024
    payload, rows, payload_bytes = make_payload(shape, target_bytes)
    baseline, peaks = asyncio.run(run_sequential(payload, calls, mode))
    result = {
        "shape": shape,
        "mode": mode,
        "target_mib": target_mib,
        "calls": calls,
        "payload_bytes": payload_bytes,
        "payload_mib": payload_bytes / 1024 / 1024,
        "rows_per_call": rows,
        "baseline_rss_high_water_bytes": baseline,
        "per_call": [
            {
                "call": index,
                "peak_rss_high_water_bytes": peak,
                "high_water_increment_bytes": max(0, peak - baseline),
            }
            for index, peak in enumerate(peaks, start=1)
        ],
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    print(json.dumps(result, sort_keys=True))


def scenarios() -> Iterable[tuple[str, int, int]]:
    for shape in SHAPES:
        for size in MATRIX_SIZES_MIB:
            for concurrency in MATRIX_CONCURRENCIES:
                yield shape, size, concurrency


def run_all() -> None:
    command_base = [sys.executable, str(Path(__file__).resolve())]
    for shape, size, concurrency in scenarios():
        completed = subprocess.run(
            [
                *command_base,
                "--shape",
                shape,
                "--target-mib",
                str(size),
                "--concurrency",
                str(concurrency),
                "--mode",
                "structured",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        print(completed.stdout, end="")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run the full isolated matrix")
    parser.add_argument("--shape", choices=SHAPES)
    parser.add_argument("--target-mib", type=int, choices=SIZES_MIB)
    parser.add_argument("--concurrency", type=int, choices=CONCURRENCIES)
    parser.add_argument("--mode", choices=MODES, default="structured")
    parser.add_argument("--sequential-calls", type=int)
    args = parser.parse_args()
    if args.all:
        if any(
            value is not None
            for value in (args.shape, args.target_mib, args.concurrency, args.sequential_calls)
        ):
            parser.error("--all cannot be combined with a single scenario")
        return args
    if args.shape is None or args.target_mib is None:
        parser.error("single scenarios require --shape and --target-mib")
    if args.sequential_calls is None and args.concurrency is None:
        parser.error("single scenarios require --concurrency or --sequential-calls")
    if args.sequential_calls is not None and args.concurrency is not None:
        parser.error("--sequential-calls and --concurrency are mutually exclusive")
    if args.sequential_calls is not None and args.sequential_calls < 1:
        parser.error("--sequential-calls must be positive")
    return args


def main() -> None:
    args = parse_args()
    if args.all:
        run_all()
    elif args.sequential_calls is not None:
        run_sequential_scenario(args.shape, args.target_mib, args.sequential_calls, args.mode)
    else:
        run_scenario(args.shape, args.target_mib, args.concurrency, args.mode)


if __name__ == "__main__":
    main()
