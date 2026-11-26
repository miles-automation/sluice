#!/usr/bin/env bash
set -u
here="$(cd "$(dirname "$0")" && pwd)"
label="$1"; py="$2"; node="${3:-node}"
langs="${LANGS:-py node}"; kill="${KILL_MB:-2048}"
out="${OUT_DIR:-$here/results}/$label.jsonl"
probes="${PROBES:-nested_replace concat_chain recursive_doubling damerau_levenshtein regexp_replace_g string_agg list_agg control_group}"
flush=(--set "SET allocator_flush_threshold = '16MB'" --set "SET allocator_bulk_deallocation_flush_threshold = '16MB'")
sup() { "$py" "$here/supervise.py" --timeout 90 --kill-mb "$kill" --out "$out" --tag "$1" -- "${@:2}"; }
worker() { if [ "$1" = py ]; then echo "$py $here/worker_py.py"; else echo "$node $here/worker_node.mjs"; fi; }
for p in $probes; do
  for lang in $langs; do
    w=$(worker "$lang")
    sup "baseline $lang $p" $w --probe "$p"
    for limit in 512 1024; do
      sup "watchdog$limit $lang $p" $w --probe "$p" --watchdog-mb "$limit"
    done
    sup "watchdog512+flush $lang $p" $w --probe "$p" --watchdog-mb 512 "${flush[@]}"
    if [ "$lang" = py ]; then
      sup "threads1 $lang $p" $w --probe "$p" --set "SET threads = 1"
    fi
  done
done
