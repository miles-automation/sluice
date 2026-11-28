import json
import os
import resource
import signal
import subprocess
import sys
import time

KILL_KB = 1024 * 1024
TIMEOUT_S = 60.0
PLAN: dict[str, list[int]] = {
    "damerau_levenshtein": [2000, 4000, 8000],
    "levenshtein": [16000, 32000, 64000],
    "editdist3": [16000, 32000, 64000],
    "jaro_similarity": [128000, 512000, 2000000],
    "jaro_winkler_similarity": [128000, 512000, 2000000],
    "hamming": [250000, 1000000, 4000000],
    "mismatches": [250000, 1000000, 4000000],
    "jaccard": [250000, 1000000, 4000000],
}


def child(function: str, length: int) -> None:
    import duckdb

    connection = duckdb.connect()
    connection.execute("SET threads=1")
    connection.execute(
        f"CREATE TABLE s AS SELECT "
        f"left(repeat('qwertyuiopasdfghjklzxcvbnm', {length} // 26 + 1), {length}) AS a, "
        f"left(repeat('mnbvcxzlkjhgfdsapoiuytrewqa', {length} // 27 + 1), {length}) AS b"
    )
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    connection.execute(f"SELECT {function}(a, b) FROM s").fetchone()
    seconds = time.monotonic() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    print(
        json.dumps(
            {
                "function": function,
                "length": length,
                "seconds": round(seconds, 4),
                "delta_mb": round((peak - base) * scale / 2**20),
                "duckdb": duckdb.__version__,
            }
        )
    )


def supervise(function: str, length: int) -> str:
    process = subprocess.Popen(
        [sys.executable, __file__, "--child", function, str(length)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    killed = None
    while process.poll() is None:
        sampled = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True
        ).stdout.strip()
        if sampled and int(sampled) > KILL_KB:
            os.kill(process.pid, signal.SIGKILL)
            killed = "memory"
        elif time.monotonic() - started > TIMEOUT_S:
            os.kill(process.pid, signal.SIGKILL)
            killed = "timeout"
        time.sleep(0.02)
    stdout, _ = process.communicate()
    return stdout.strip() or json.dumps({"function": function, "length": length, "killed": killed})


def main() -> None:
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], int(sys.argv[3]))
        return
    only = set(sys.argv[1:])
    for function, lengths in PLAN.items():
        if only and function not in only:
            continue
        for length in lengths:
            line = supervise(function, length)
            print(line, flush=True)
            if "killed" in line:
                break


if __name__ == "__main__":
    main()
