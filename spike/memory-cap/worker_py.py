import argparse
import json
import resource
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duckdb  # noqa: E402
from memsample import release_allocator, self_sample  # noqa: E402

MB = 2**20

LOCKDOWN = (
    "SET temp_directory = ''",
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    "SET max_memory = '{max_memory}'",
)


def open_store(max_memory: str, extra: list[str]) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE t AS SELECT i::INTEGER AS i, 'a' AS s FROM range(100) r(i)")
    for statement in LOCKDOWN:
        connection.execute(statement.format(max_memory=max_memory))
    for statement in extra:
        connection.execute(statement)
    connection.execute("SET lock_configuration = true")
    return connection


def mb(value: int) -> int:
    return round(value / MB)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", required=True)
    parser.add_argument("--probes", default=str(Path(__file__).with_name("probes.json")))
    parser.add_argument("--max-memory", default="256MB")
    parser.add_argument("--watchdog-mb", type=int, default=0)
    parser.add_argument("--interval-ms", type=float, default=20.0)
    parser.add_argument("--metric", choices=["rss", "footprint"], default="rss")
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--rlimit-as-mb", type=int, default=0)
    parser.add_argument("--rlimit-data-mb", type=int, default=0)
    args = parser.parse_args()
    sql = json.loads(Path(args.probes).read_text())[args.probe]

    report: dict[str, object] = {"lang": "python", "duckdb": duckdb.__version__, "probe": args.probe}
    connection = open_store(args.max_memory, args.set)
    report["rss_setup_mb"] = mb(self_sample()[0])
    if args.rlimit_as_mb:
        resource.setrlimit(resource.RLIMIT_AS, (args.rlimit_as_mb * MB, args.rlimit_as_mb * MB))
        report["rlimit_as"] = resource.getrlimit(resource.RLIMIT_AS)
    if args.rlimit_data_mb:
        resource.setrlimit(resource.RLIMIT_DATA, (args.rlimit_data_mb * MB, args.rlimit_data_mb * MB))
        report["rlimit_data"] = resource.getrlimit(resource.RLIMIT_DATA)

    cursor = connection.cursor()
    done = threading.Event()
    state: dict[str, float | int | None] = {"peak": 0, "fired_at": None, "fired_rss": None, "samples": 0}
    index = 0 if args.metric == "rss" else 1

    def watchdog() -> None:
        while not done.is_set():
            value = self_sample()[index]
            state["samples"] = int(state["samples"] or 0) + 1
            state["peak"] = max(int(state["peak"] or 0), value)
            if args.watchdog_mb and state["fired_at"] is None and value > args.watchdog_mb * MB:
                state["fired_at"] = time.monotonic()
                state["fired_rss"] = value
                cursor.interrupt()
            done.wait(args.interval_ms / 1000)

    thread = threading.Thread(target=watchdog, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        rows = cursor.execute(sql).fetchall()
        report["outcome"] = "completed"
        report["result"] = str(rows)[:80]
    except duckdb.OutOfMemoryException as exc:
        report["outcome"] = "duckdb_oom"
        report["error"] = str(exc)[:200]
    except duckdb.InterruptException as exc:
        report["outcome"] = "interrupted"
        report["error"] = str(exc)[:200]
    except MemoryError as exc:
        report["outcome"] = "python_memoryerror"
        report["error"] = str(exc)[:200]
    except duckdb.Error as exc:
        report["outcome"] = f"duckdb_error:{type(exc).__name__}"
        report["error"] = str(exc)[:200]
    ended = time.monotonic()
    done.set()
    thread.join()
    cursor.close()
    report["query_s"] = round(ended - started, 2)
    report["watchdog_samples"] = state["samples"]
    report["watchdog_peak_mb"] = mb(int(state["peak"] or 0))
    if state["fired_at"] is not None:
        report["interrupt_rss_mb"] = mb(int(state["fired_rss"] or 0))
        report["interrupt_after_s"] = round(float(state["fired_at"]) - started, 2)
        report["interrupt_to_return_s"] = round(ended - float(state["fired_at"]), 2)
    report["rss_after_mb"] = mb(self_sample()[0])

    try:
        check = connection.cursor()
        normal = check.execute("SELECT count(*), sum(i) FROM t").fetchall()
        check.execute("CREATE TABLE z AS SELECT i, s || i AS s FROM t")
        check.execute("DROP TABLE z")
        check.close()
        report["usable"] = normal == [(100, 4950)]
    except Exception as exc:  # noqa: BLE001
        report["usable"] = False
        report["usable_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    time.sleep(1.0)
    report["rss_after_1s_mb"] = mb(self_sample()[0])
    report["release"] = release_allocator()
    report["rss_after_release_mb"] = mb(self_sample()[0])
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
