import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memsample import sample  # noqa: E402

HARD_KILL_BYTES = 2 * 1024**3
INTERVAL = 0.02


def run(command: list[str], timeout: float, kill_bytes: int) -> dict[str, object]:
    started = time.monotonic()
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak_rss = 0
    peak_footprint = 0
    killed: str | None = None
    while child.poll() is None:
        value = sample(child.pid)
        if value is not None:
            rss, footprint = value
            peak_rss = max(peak_rss, rss)
            peak_footprint = max(peak_footprint, footprint)
            if max(rss, footprint) > kill_bytes:
                os.kill(child.pid, signal.SIGKILL)
                killed = "supervisor_memory"
        if killed is None and time.monotonic() - started > timeout:
            os.kill(child.pid, signal.SIGKILL)
            killed = "supervisor_timeout"
        time.sleep(INTERVAL)
    stdout, stderr = child.communicate()
    wall = time.monotonic() - started
    report: dict[str, object] | None = None
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                report = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return {
        "command": command,
        "returncode": child.returncode,
        "killed": killed,
        "peak_rss_mb": round(peak_rss / 2**20),
        "peak_footprint_mb": round(peak_footprint / 2**20),
        "wall_s": round(wall, 2),
        "child": report,
        "stderr_tail": stderr[-600:],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--kill-mb", type=int, default=HARD_KILL_BYTES // 2**20)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    kill = min(args.kill_mb * 2**20, HARD_KILL_BYTES)
    result = run(command, args.timeout, kill)
    result["tag"] = args.tag
    with open(args.out, "a") as handle:
        handle.write(json.dumps(result) + "\n")
    child = result["child"] or {}
    print(
        f"{args.tag:60s} peak_rss={result['peak_rss_mb']}MB fp={result['peak_footprint_mb']}MB "
        f"wall={result['wall_s']}s killed={result['killed']} rc={result['returncode']} "
        f"outcome={child.get('outcome')} usable={child.get('usable')} "
        f"irq_rss={child.get('interrupt_rss_mb')} after={child.get('rss_after_mb')}/{child.get('rss_after_release_mb')}"
    )


if __name__ == "__main__":
    main()
