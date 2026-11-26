#!/usr/bin/env bash
set -u
here="$(cd "$(dirname "$0")" && pwd)"
label="$1"; py="$2"; node="${3:-node}"
for p in nested_replace recursive_doubling regexp_replace_g damerau_levenshtein; do
  for lang in ${LANGS:-py node}; do
    if [ "$lang" = py ]; then cmd=("$py" "$here/repeat_py.py" "$p"); else cmd=("$node" "$here/repeat_node.mjs" "$p"); fi
    "$py" "$here/supervise.py" --timeout 120 --kill-mb "${KILL_MB:-2048}" --out "$here/results/$label.jsonl" --tag "repeat4 $lang $p" -- "${cmd[@]}"
  done
done
