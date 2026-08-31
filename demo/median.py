"""The M5 demo: does an agent get a median right with vs without Sluice?

Not a test, and deliberately **not part of CI** (plan/001-scratch-db.md,
"Not in CI"; DoD item 5). It drives a real model through the installed
`claude` CLI, so it is non-deterministic on every axis that matters: model
sampling, which tool the agent chooses, how it phrases its final answer, and
CLI/model version drift can each change the outcome between runs. One run
that comes out red-then-green is evidence, not a guarantee, and this script
never asserts an outcome -- it only records what was actually observed.

Two conditions, same prompt, same 400 deterministic rows, same `score`
column (`tests/fake_server/server.py::rows_payload`):

  baseline  - the `claude` CLI talks directly to `tests.fake_server` over
              stdio. The agent receives the full raw JSON payload as the
              tool result and has to compute the median itself.
  treatment - the `claude` CLI talks to Sluice, configured around that same
              fake server. The agent receives a short preview plus a table
              handle and a `query` tool; if it uses `query`, DuckDB computes
              the median exactly instead of the model eyeballing it.

The "right" answer is computed mechanically in this process from the same
`rows_payload(n)` function the fake server itself calls -- never guessed,
never hand-copied.

Usage:
    uv run python -m demo.median
    uv run python -m demo.median --model claude-haiku-4-5-20251001
    uv run python -m demo.median --rows 400 --timeout 180 --max-turns 8
    uv run python -m demo.median --dry-run   # build configs, skip invoking claude

Requires the `claude` CLI (https://claude.com/claude-code) on PATH, already
authenticated independently of this repo -- this script does not read, set,
or forward any credential. It writes one JSON report and one transcript file
per condition into `demo/transcripts/<UTC-timestamp>/`.

This script never fabricates a result. If `claude` cannot be invoked in the
current environment -- missing binary, a non-interactive/sandboxed session
with no way to grant tool-call approval, a timeout, a non-zero exit before an
answer was produced -- the affected condition is recorded with
`"status": "pending"` and the raw failure, never a guessed answer. Do not
edit a pending report to look like a completed run.
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _relpath(path: Path) -> str:
    """Display path relative to the repo root when possible, absolute otherwise.

    `--out-dir` is user-configurable, so a report or transcript can land
    outside REPO_ROOT; `Path.relative_to` raises in that case rather than
    falling back, which would crash a run purely on its way to a summary.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def prompt_for_rows(rows: int) -> str:
    """Keep the requested row count and mechanically graded payload aligned."""
    return (
        "You have access to a tool that fetches rows of data; each row has a "
        f"numeric 'score' field. Call it requesting {rows} rows. Then compute the "
        f"median of the 'score' field across all {rows} rows. Reply with ONLY the "
        "numeric median value and nothing else -- no units, no explanation."
    )


NON_DETERMINISM_NOTE = (
    "This is a single sampled run of a live model, not a deterministic test. "
    "`claude` may pick a different tool, retry differently, or phrase its "
    "final numeric answer differently on every invocation, and CLI/model "
    "updates change behavior out from under a pinned prompt. Do not treat one "
    "green run as proof of anything beyond that run; the repo does not gate "
    "CI on this file for exactly that reason "
    "(plan/001-scratch-db.md, 'Not in CI')."
)

# Best-effort defense in depth. The demo config itself needs no credential --
# the fake server takes none -- so nothing here *should* ever fire. This
# exists in case `claude`'s own stderr/diagnostics echo something
# credential-shaped (e.g. from its own auth layer) that would otherwise land
# in a transcript saved into the repo.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{8,}"
    ),
]


def scrub(text: str) -> str:
    """Redact credential-shaped substrings before anything is written to disk."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def expected_median(n: int) -> float:
    """The mechanically-correct answer, from the same payload the server serves."""
    from tests.fake_server.server import rows_payload

    rows = rows_payload(n)
    return float(statistics.median(float(row["score"]) for row in rows))


def _completed_text(value: str | bytes | None) -> str:
    """Normalize TimeoutExpired's platform-dependent captured output type."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _write_mcp_config(path: Path, servers: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


def _fake_server_entry() -> dict[str, Any]:
    return {
        "command": sys.executable,
        "args": ["-m", "tests.fake_server"],
        "cwd": str(REPO_ROOT),
    }


def _write_sluice_config(path: Path) -> None:
    """A minimal sluice.toml proxying the same fake server as the baseline."""
    path.write_text(
        "[servers.fake]\n"
        f'command = "{sys.executable}"\n'
        'args = ["-m", "tests.fake_server"]\n'
        f'cwd = "{REPO_ROOT}"\n'
        "\n[limits]\n"
        "preview_rows = 3\n",
        encoding="utf-8",
    )


def _allowed_tools_baseline() -> list[str]:
    return ["mcp__fake__rows"]


def _allowed_tools_treatment() -> list[str]:
    from sluice.naming import mounted_name

    return [f"mcp__sluice__{mounted_name('fake', 'rows')}", "mcp__sluice__query"]


@dataclass
class ConditionResult:
    name: str
    argv: list[str]
    status: str  # "ok" (claude ran to completion) | "pending" (no answer observed)
    reason: str | None
    exit_code: int | None
    timed_out: bool
    duration_seconds: float | None
    model_used: str | None
    parsed_answer: float | None
    correct: bool | None
    transcript_path: str | None


def _extract_result_text(stdout: str) -> tuple[str | None, str | None]:
    """Pull the final result and model out of Claude's JSON event stream.

    The stream is also the raw transcript: it contains tool calls and results,
    unlike the CLI's single-result JSON format. Malformed or future event lines
    are ignored because stdout is preserved for manual inspection.
    """
    result: str | None = None
    model: str | None = None
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        candidate_model = payload.get("model")
        if isinstance(candidate_model, str):
            model = candidate_model
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            model = message["model"]
        candidate_result = payload.get("result")
        if payload.get("type") == "result" and isinstance(candidate_result, str):
            result = candidate_result
    return result, model


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_numeric_answer(text: str) -> float | None:
    match = _NUMBER.search(text.strip())
    if match is None:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _run_condition(
    *,
    name: str,
    prompt: str,
    mcp_config_path: Path,
    allowed_tools: list[str],
    model: str | None,
    max_turns: int,
    timeout: float,
    expected: float,
    tolerance_rel: float,
    tolerance_abs: float,
    out_dir: Path,
) -> ConditionResult:
    # Sluice has one extra startup hop while it discovers the downstream
    # server. Claude Code represents not-yet-connected MCP tools as deferred;
    # ToolSearch is the narrowly scoped built-in that waits for and loads them.
    cli_tools = ["ToolSearch", *allowed_tools]
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--tools",
        ",".join(cli_tools),
        "--allowedTools",
        ",".join(cli_tools),
        "--max-turns",
        str(max_turns),
    ]
    if model:
        argv += ["--model", model]

    transcript_path = out_dir / f"{name}.txt"
    start = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return ConditionResult(
            name=name,
            argv=argv,
            status="pending",
            reason=f"`claude` not invocable: {exc}",
            exit_code=None,
            timed_out=False,
            duration_seconds=None,
            model_used=None,
            parsed_answer=None,
            correct=None,
            transcript_path=None,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        raw = scrub(
            _completed_text(exc.stdout) + "\n--- stderr ---\n" + _completed_text(exc.stderr)
        )
        transcript_path.write_text(raw, encoding="utf-8")
        return ConditionResult(
            name=name,
            argv=argv,
            status="pending",
            reason=f"timed out after {timeout}s",
            exit_code=None,
            timed_out=True,
            duration_seconds=duration,
            model_used=None,
            parsed_answer=None,
            correct=None,
            transcript_path=_relpath(transcript_path),
        )
    duration = time.monotonic() - start

    raw = scrub(completed.stdout + "\n--- stderr ---\n" + completed.stderr)
    transcript_path.write_text(raw, encoding="utf-8")

    if completed.returncode != 0:
        return ConditionResult(
            name=name,
            argv=argv,
            status="pending",
            reason=(
                f"claude exited {completed.returncode} before producing an answer; "
                "see transcript. In a sandboxed/non-interactive session this is "
                "commonly a blocked tool-call approval, not a demo bug."
            ),
            exit_code=completed.returncode,
            timed_out=False,
            duration_seconds=duration,
            model_used=None,
            parsed_answer=None,
            correct=None,
            transcript_path=_relpath(transcript_path),
        )

    result_text, model_used = _extract_result_text(completed.stdout)
    if result_text is None:
        return ConditionResult(
            name=name,
            argv=argv,
            status="pending",
            reason=(
                "claude exited 0 but its stream-json result event did not parse; see transcript"
            ),
            exit_code=0,
            timed_out=False,
            duration_seconds=duration,
            model_used=model_used,
            parsed_answer=None,
            correct=None,
            transcript_path=_relpath(transcript_path),
        )

    answer = _parse_numeric_answer(result_text)
    if answer is None:
        return ConditionResult(
            name=name,
            argv=argv,
            status="ok",
            reason="ran to completion but no numeric answer could be parsed from the reply",
            exit_code=0,
            timed_out=False,
            duration_seconds=duration,
            model_used=model_used,
            parsed_answer=None,
            correct=None,
            transcript_path=_relpath(transcript_path),
        )

    correct = abs(answer - expected) <= max(tolerance_abs, tolerance_rel * abs(expected))
    return ConditionResult(
        name=name,
        argv=argv,
        status="ok",
        reason=None,
        exit_code=0,
        timed_out=False,
        duration_seconds=duration,
        model_used=model_used,
        parsed_answer=answer,
        correct=correct,
        transcript_path=_relpath(transcript_path),
    )


def _pending_condition(name: str, argv: list[str], reason: str) -> ConditionResult:
    return ConditionResult(
        name=name,
        argv=argv,
        status="pending",
        reason=reason,
        exit_code=None,
        timed_out=False,
        duration_seconds=None,
        model_used=None,
        parsed_answer=None,
        correct=None,
        transcript_path=None,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rows", type=int, default=400)
    parser.add_argument(
        "--model", default="sonnet", help="passed to `claude --model` (default: sonnet)"
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per condition")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--tolerance-rel", type=float, default=1e-6)
    parser.add_argument("--tolerance-abs", type=float, default=0.005)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "demo" / "transcripts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build configs and compute the expected median; never invoke claude",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.rows < 1:
        raise SystemExit("--rows must be positive")
    prompt = prompt_for_rows(args.rows)
    expected = expected_median(args.rows)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sluice-demo-") as tmp:
        tmp_path = Path(tmp)

        baseline_config = tmp_path / "baseline.mcp.json"
        _write_mcp_config(baseline_config, {"fake": _fake_server_entry()})

        sluice_toml = tmp_path / "sluice.toml"
        _write_sluice_config(sluice_toml)
        treatment_config = tmp_path / "treatment.mcp.json"
        _write_mcp_config(
            treatment_config,
            {
                "sluice": {
                    "command": sys.executable,
                    "args": ["-m", "sluice", "--config", str(sluice_toml)],
                    "cwd": str(REPO_ROOT),
                }
            },
        )

        common = {
            "model": args.model,
            "prompt": prompt,
            "max_turns": args.max_turns,
            "timeout": args.timeout,
            "expected": expected,
            "tolerance_rel": args.tolerance_rel,
            "tolerance_abs": args.tolerance_abs,
            "out_dir": out_dir,
        }

        baseline_argv_preview = ["claude", "-p", prompt, "--mcp-config", str(baseline_config)]
        treatment_argv_preview = ["claude", "-p", prompt, "--mcp-config", str(treatment_config)]

        if args.dry_run:
            baseline = _pending_condition(
                "baseline", baseline_argv_preview, "dry run requested: claude was not invoked"
            )
            treatment = _pending_condition(
                "treatment", treatment_argv_preview, "dry run requested: claude was not invoked"
            )
        else:
            baseline = _run_condition(
                name="baseline",
                mcp_config_path=baseline_config,
                allowed_tools=_allowed_tools_baseline(),
                **common,
            )
            treatment = _run_condition(
                name="treatment",
                mcp_config_path=treatment_config,
                allowed_tools=_allowed_tools_treatment(),
                **common,
            )

    report = {
        "demo": "sluice median comparison (plan/001-scratch-db.md, M5, DoD 5)",
        "generated_at_utc": run_id,
        "rows": args.rows,
        "prompt": prompt,
        "model_requested": args.model,
        "expected_median": expected,
        "tolerance": {"rel_tol": args.tolerance_rel, "abs_tol": args.tolerance_abs},
        "conditions": {"baseline": asdict(baseline), "treatment": asdict(treatment)},
        "non_determinism_note": NON_DETERMINISM_NOTE,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    _print_summary(report, report_path)

    # A model getting the wrong answer, or `claude` being unreachable in this
    # environment, is a valid, recorded outcome -- not a harness failure.
    # Exit 0 whenever a report was produced; only an unhandled exception
    # above (a genuine harness bug) should ever surface as a nonzero exit.
    return 0


def _print_summary(report: dict[str, Any], report_path: Path) -> None:
    print(f"expected median: {report['expected_median']}")
    for name in ("baseline", "treatment"):
        cond = report["conditions"][name]
        if cond["status"] == "pending":
            print(f"{name}: PENDING -- {cond['reason']}")
        else:
            print(
                f"{name}: status={cond['status']} model={cond['model_used']} "
                f"answer={cond['parsed_answer']} correct={cond['correct']}"
            )
    print(f"report: {_relpath(report_path)}")
    print(NON_DETERMINISM_NOTE)


if __name__ == "__main__":
    raise SystemExit(main())
