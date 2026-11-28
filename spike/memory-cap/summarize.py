import json
import sys
from pathlib import Path

RESULTS = Path(__file__).with_name("results")
FILES = {
    "macos": ["macos-arm64.jsonl", "macos-arm64-extra.jsonl"],
    "linux": [
        "linux-arm64-docker-py.jsonl",
        "linux-arm64-docker-node.jsonl",
        "linux-arm64-docker-extra.jsonl",
        "linux-arm64-docker-rlimit-py.jsonl",
        "linux-arm64-docker-rlimit-node.jsonl",
    ],
}
SHORT = {
    "completed": "done",
    "duckdb_oom": "OOM",
    "interrupted": "irq",
}


def cell(record: dict[str, object]) -> str:
    child = record.get("child") or {}
    assert isinstance(child, dict)
    peak = record["peak_rss_mb"]
    if record.get("killed"):
        outcome = "KILLED(sup)"
    elif record["returncode"] == -6:
        outcome = "ABORT"
    elif record["returncode"] != 0:
        outcome = f"exit{record['returncode']}"
    else:
        outcome = SHORT.get(str(child.get("outcome")), str(child.get("outcome")))
    text = f"{peak} {outcome}"
    if child.get("interrupt_rss_mb") is not None:
        text += f" @{child['interrupt_rss_mb']}"
    if child.get("usable") is False:
        text += " UNUSABLE"
    return text


def load(platform: str) -> dict[tuple[str, str, str], dict[str, object]]:
    table: dict[tuple[str, str, str], dict[str, object]] = {}
    for name in FILES[platform]:
        path = RESULTS / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            record = json.loads(line)
            mechanism, lang, probe = str(record["tag"]).split(" ")
            table[(probe, mechanism, lang)] = record
    return table


def main() -> None:
    probes = [
        "nested_replace", "nested_replace_tiny_literals", "concat_chain", "recursive_doubling",
        "damerau_levenshtein", "regexp_replace_g", "truncated_wrapper", "string_agg", "list_agg",
        "control_group",
    ]
    mechanisms = ["baseline", "threads1", "watchdog512", "watchdog1024", "watchdog512+flush",
                  "rlimit_data1024", "rlimit_data1536", "rlimit_as1024", "rlimit_as1536"]
    out: list[str] = []
    for platform in ("macos", "linux"):
        table = load(platform)
        for lang in ("py", "node"):
            present = [m for m in mechanisms if any((p, m, lang) in table for p in probes)]
            out.append(f"\n### {platform} arm64, {'Python' if lang == 'py' else 'Node'}\n")
            out.append("| probe | " + " | ".join(present) + " |")
            out.append("|---" * (len(present) + 1) + "|")
            for probe in probes:
                cells = [cell(table[(probe, m, lang)]) if (probe, m, lang) in table else "" for m in present]
                out.append(f"| {probe} | " + " | ".join(cells) + " |")
    text = "\n".join(out) + "\n"
    (RESULTS / "summary-tables.txt").write_text(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
