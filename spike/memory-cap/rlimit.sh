#!/usr/bin/env bash
set -u
here="$(cd "$(dirname "$0")" && pwd)"
label="$1"; py="$2"; langs="${LANGS:-py}"
out="$here/results/$label.jsonl"
probes="${PROBES:-nested_replace concat_chain recursive_doubling damerau_levenshtein regexp_replace_g control_group}"
sup() { "$py" "$here/supervise.py" --timeout 90 --kill-mb 1900 --out "$out" --tag "$1" -- "${@:2}"; }
for p in $probes; do
  for mb in 1024 1536; do
    if [ "$langs" = py ]; then
      sup "rlimit_as$mb py $p" "$py" "$here/worker_py.py" --probe "$p" --rlimit-as-mb "$mb"
      sup "rlimit_data$mb py $p" "$py" "$here/worker_py.py" --probe "$p" --rlimit-data-mb "$mb"
    else
      kb=$((mb * 1024))
      sup "rlimit_as$mb node $p" bash -c "ulimit -v $kb && exec node $here/worker_node.mjs --probe $p"
      sup "rlimit_data$mb node $p" bash -c "ulimit -d $kb && exec node $here/worker_node.mjs --probe $p"
    fi
  done
done
