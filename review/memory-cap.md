# Bounding Sluice's process memory beyond `max_memory` (task 1223, spike)

Date: 2026-11-26. This is a time-boxed measurement, not an implementation. Scripts and
raw results: `spike/memory-cap/`. Engines measured: Python `duckdb` 2.0.1 and
`@duckdb/node-api` 2.0.1-r.1 (engine v2.0.1), on macOS 26 arm64 (Python 3.14.2,
Node 25.3.0) and on Linux arm64 in Docker (`python:3.14`, `node:24` = 24.22.3,
glibc 2.41, `--memory=2g --memory-swap=2g`).

## How it was measured

- `supervise.py` spawns every probe in a child, samples RSS and (on macOS) the physical
  footprint every 20 ms through `proc_pid_rusage` or `/proc/<pid>/statm`, SIGKILLs at a
  hard 2 GiB (1900 MiB inside Docker, so the cgroup never fires first), and records peak,
  wall time and outcome. Probes ran one at a time; nothing ran unsupervised.
- `worker_py.py` and `worker_node.mjs` open an in-memory store with the same lockdown
  as `store.py` / `store.ts` (`temp_directory=''`, external access off,
  `max_memory='256MB'`, then `lock_configuration=true`), load a 100-row table `t(i, s='a')`
  standing in for a materialized result, run one probe, then check the store with a
  normal query plus a `CREATE TABLE`/`DROP TABLE` (`usable`), and record RSS
  afterwards, after 1 s and after an allocator release call.
- Watchdog mode: a thread (Python) or `setInterval` (Node) samples the process's own RSS
  every 20 ms and calls `cursor.interrupt()` / `connection.interrupt()` once RSS exceeds
  the limit. DuckDB releases the GIL during execution, so the Python thread did sample
  (5 to 200 samples per query, `watchdog_samples` in the raw files).
- Every probe is admitted by the real `sluice.gate.check` on DuckDB 2.0.1
  (`results/gate-python-duckdb2.0.1.json`, script `gatecheck.py`). Probe SQL is generated
  by `probes.py` into `probes.json`; scales were chosen so natural peaks are 1 to 2 GiB.
- Sweeps: `sweep.sh` (baseline, `threads=1`, watchdog 512 MB and 1 GB, watchdog 512 MB
  with `allocator_flush_threshold` and `allocator_bulk_deallocation_flush_threshold` set
  to 16MB before the lock), `rlimit.sh` (Linux RLIMIT_AS / RLIMIT_DATA at 1024 and
  1536 MB), `repeat.sh` (same probe four times in one process), `close_py.py`
  (close and reopen the database), `mallinfo_py.py` and `held_py.py` (where the memory
  sits). `summarize.py` builds the tables below into `results/summary-tables.txt`.

### Probes

| probe | what it does | SQL size |
|---|---|---|
| nested_replace | three nested `replace(s,'a',<250 a>)` over 100 rows | 832 B |
| nested_replace_tiny_literals | 13 nested `replace(s,'a','aaaa')` over 20 rows | 327 B, longest literal 4 B |
| concat_chain | 7 CTEs, each `s\|\|s\|\|s\|\|s`, from an 800-char seed | 1.2 KB |
| recursive_doubling | recursive CTE `s\|\|s` to n=23, cross-joined with `t` | 133 B |
| damerau_levenshtein | `damerau_levenshtein` on a 14,000-char string | 1.2 KB |
| regexp_replace_g | `regexp_replace(s\|\|<1000>, '', <12,000 y>, 'g')` over 100 rows | 13 KB |
| truncated_wrapper | nested_replace without the aggregate, wrapped in `SELECT left(x,100)` | 856 B |
| string_agg | `string_agg` of 1M 1-KB rows (three-way self cross join) | 1.1 KB |
| list_agg | `list()` over a 100M-row four-way cross join | 63 B |
| control_group | `GROUP BY` over 1M 500-byte keys (should hit `max_memory`) | 609 B |

## Results

Cell format: `peak RSS MB, outcome`, `@N` = RSS in MB when the watchdog called
interrupt. `done` = query completed, `OOM` = DuckDB Out of Memory error, `irq` =
interrupted, `KILLED(sup)` = supervisor kill at the hard cap, `ABORT` = SIGABRT,
`exit1` = process could not start or run. Every surviving run left the store usable
except where marked `UNUSABLE`. Sources: `results/macos-arm64*.jsonl`,
`results/linux-arm64-docker-*.jsonl`.

### macOS arm64, Python

| probe | baseline | threads=1 | watchdog 512 | watchdog 1024 | watchdog 512 + flush settings |
|---|---|---|---|---|---|
| nested_replace | 1540 done | 1517 done | 1487 irq @656 | 1522 irq @1155 | 1443 irq @712 |
| nested_replace_tiny_literals | 1800 done | 1899 done | 1897 irq @519 | 1899 irq @1062 | 1895 irq @518 |
| concat_chain | 774 OOM | 774 OOM | 774 OOM @689 | 774 OOM | 774 OOM @695 |
| recursive_doubling | 1105 done | 1105 done | **534 irq @533** | 1109 irq @1025 | **538 irq @537** |
| damerau_levenshtein | 1698 done | 1838 done | 1597 irq @827 | 1597 irq @1324 | 1597 irq @809 |
| regexp_replace_g | 1223 done | 1224 done | 1223 irq @521 | 1223 irq @1028 | 1225 irq @522 |
| truncated_wrapper | 325 OOM | 325 OOM | 325 OOM | 329 OOM | 325 OOM |
| string_agg | 188 OOM | 188 OOM | 188 OOM | 188 OOM | 188 OOM |
| list_agg | 299 OOM | 298 OOM | 299 OOM | 299 OOM | 299 OOM |
| control_group | 303 OOM | 308 OOM | 303 OOM | 303 OOM | 303 OOM |

### macOS arm64, Node

| probe | baseline | watchdog 512 | watchdog 1024 | watchdog 512 + flush settings |
|---|---|---|---|---|
| nested_replace | 1540 done | 1502 irq @616 | 1587 irq @1153 | 1515 irq @616 |
| nested_replace_tiny_literals | 1920 done | 1900 irq @513 | 1915 irq @1025 | 1919 irq @513 |
| concat_chain | 797 OOM | 798 OOM @613 | 797 OOM | 798 OOM @607 |
| recursive_doubling | 1131 done | **558 irq @557** | 1131 irq @1131 | **560 irq @559** |
| damerau_levenshtein | 1648 done | 1621 irq @795 | 1621 irq @1162 | 1621 irq @795 |
| regexp_replace_g | 1246 done | 1246 irq @522 | 1246 irq @1028 | 1257 irq @522 |
| truncated_wrapper | 349 OOM | 352 OOM | 349 OOM | 349 OOM |
| string_agg | 211 OOM | 211 OOM | 211 OOM | 211 OOM |
| list_agg | 322 OOM | 322 OOM | 322 OOM | 322 OOM |
| control_group | 326 OOM | 326 OOM | 326 OOM | 326 OOM |

### Linux arm64 (Docker), Python

| probe | baseline | threads=1 | watchdog 512 | watchdog 1024 | wd 512 + flush | RLIMIT_DATA 1024 | RLIMIT_DATA 1536 | RLIMIT_AS 1024 / 1536 |
|---|---|---|---|---|---|---|---|---|
| nested_replace | 1545 done | 1460 done | 1501 irq @608 | 1422 irq @1106 | 1479 irq @692 | 497 OOM | 978 OOM | exit1 / exit1 |
| nested_replace_tiny_literals | 1816 done | 1865 done | 1809 irq @515 | 1879 irq @1048 | 1818 irq @549 | | | |
| concat_chain | 793 OOM | 791 OOM | 794 OOM @782 | 793 OOM | 794 irq @682 | 491 OOM | 794 OOM | exit1 / exit1 |
| recursive_doubling | 1103 done | 1102 done | **547 irq @541** | 1103 irq @1102 | **547 irq @541** | 474 OOM **UNUSABLE** | 761 OOM | exit1 / exit1 |
| damerau_levenshtein | 1573 done | 1572 done | 1430 irq @846 | 1408 irq @1092 | 1398 irq @917 | 521 OOM | 1084 OOM | exit1 / exit1 |
| regexp_replace_g | 1239 done | 1241 done | 1239 irq @523 | 1266 irq @1033 | 1263 irq @523 | 402 OOM | 749 OOM | exit1 / exit1 |
| truncated_wrapper | 369 OOM | 318 OOM | 369 OOM | 348 OOM | 354 OOM | | | |
| string_agg | 206 OOM | 207 OOM | 206 OOM | 205 OOM | 205 OOM | | | |
| list_agg | 320 OOM | 269 OOM | 317 OOM | 320 OOM | 320 OOM | | | |
| control_group | 316 OOM | 312 OOM | 260 OOM | 272 OOM | 285 OOM | 315 OOM | 269 OOM | exit1 / exit1 |

### Linux arm64 (Docker), Node

| probe | baseline | watchdog 512 | watchdog 1024 | wd 512 + flush | RLIMIT_DATA 1024 | RLIMIT_DATA 1536 | RLIMIT_AS 1024 / 1536 |
|---|---|---|---|---|---|---|---|
| nested_replace | 1594 done | 1655 irq @517 | 1584 irq @1070 | 1437 irq @575 | 239 OOM | 702 OOM | exit1 / exit1 |
| nested_replace_tiny_literals | 1900 KILLED(sup) | 1915 KILLED(sup) | 1857 irq @1087 | 1855 irq @514 | | | |
| concat_chain | 831 OOM | 830 OOM | 831 OOM | 834 OOM | 229 OOM | 687 OOM | exit1 / exit127 |
| recursive_doubling | 1143 done | **586 irq @579** | 1143 irq @1106 | **587 irq @579** | 248 OOM | **634 ABORT** | exit1 / exit1 |
| damerau_levenshtein | 1612 done | 1364 irq @679 | 1517 irq @1067 | 1357 irq @682 | **154 ABORT** | 707 OOM | exit1 / exit1 |
| regexp_replace_g | 1278 done | 1278 irq @516 | 1291 irq @1037 | 1291 irq @528 | 226 OOM | 571 OOM | exit1 / exit1 |
| truncated_wrapper | 389 OOM | 389 OOM | 390 OOM | 382 OOM | | | |
| string_agg | 248 OOM | 248 OOM | 248 OOM | 245 OOM | | | |
| list_agg | 359 OOM | 359 OOM | 359 OOM | 359 OOM | | | |
| control_group | 342 OOM | 309 OOM | 312 OOM | 312 OOM | **268 ABORT** | 344 OOM | exit1 / exit1 |

## Findings

### 1. Baseline under `max_memory=256MB`

- Six string-amplification shapes escape `max_memory`: nested_replace (1.5 GB),
  nested_replace_tiny_literals (1.8 to 1.9 GB), recursive_doubling (1.1 GB),
  damerau_levenshtein (1.6 to 1.7 GB), regexp_replace_g (1.2 to 1.3 GB). All complete
  with a correct result; DuckDB never raises. Scaling them up is linear or exponential in
  the SQL (one more nesting level of nested_replace_tiny_literals is 4x), so the earlier
  11 to 13 GiB kills are expected, not outliers.
- Shapes whose strings end up in a buffer-managed structure are caught, with overshoot:
  concat_chain OOMs only at 774 to 831 MB RSS (3x the limit); truncated_wrapper,
  string_agg, list_agg and control_group OOM at 190 to 390 MB.
- Python and Node behave the same; they run the same engine. macOS and Linux agree
  within about 10%.
- `threads=1` changes nothing (same peaks, same outcomes).

### 2. In-process RSS watchdog

- The interrupt always lands (every watchdog run that crossed the limit ended
  `interrupted` or with DuckDB's own OOM) and the store stayed usable in every watchdog
  run, on both platforms and both languages.
- It bounds memory only for **multi-step** growth. recursive_doubling stopped at
  534 to 587 MB against a 512 MB limit (overshoot under 80 MB, interrupt-to-return
  40 to 60 ms), because each recursion step is a separate pipeline with an interrupt
  check between them.
- It does **not** bound single-operator amplification. For nested_replace,
  nested_replace_tiny_literals, damerau_levenshtein and regexp_replace_g the interrupt
  fired at 513 to 917 MB and the process still reached its natural peak (1.4 to 1.9 GB),
  because all 100 rows fit in one 2048-row vector and the whole expression is evaluated
  before the next interrupt check. regexp_replace_g kept running 1.5 to 3 s after the
  interrupt (`interrupt_to_return_s`), finished the vector, and only then reported
  interrupted. damerau_levenshtein returned within 0.1 s of the interrupt but had
  already reached 1.4 to 1.6 GB by then. Node on
  Linux hit the 1.9 GiB supervisor cap with the watchdog armed at 512 MB
  (nested_replace_tiny_literals). Overshoot here is unbounded: it is whatever one
  vector's worth of the expression allocates, and the SQL controls that.
- Allocator release does not work in-process:
  - macOS: RSS stays at 0.6 to 1.4 GB after the query; `malloc_zone_pressure_relief`
    freed 0 bytes; closing the DuckDB database does not lower RSS either
    (`results/macos-arm64-close.jsonl`). The memory is reused, though: running the same
    probe four times plateaus (`results/macos-arm64-repeat.jsonl`, e.g. 1014, 1014,
    1014, 1022 MB).
  - Linux: RSS stays at 1.0 to 1.4 GB after string probes; `malloc_trim(0)`,
    `MALLOC_MMAP_THRESHOLD_=131072`, `MALLOC_ARENA_MAX=1|2` do not release it, and
    `duckdb_memory()` reports nothing held. glibc's main arena is 19 to 50 MB
    (`results/linux-arm64-docker-mallinfo.jsonl`, `-held.jsonl`). **Closing the
    database drops RSS to 69 MB** (`results/linux-arm64-docker-close.jsonl`). Worse,
    the memory is not reused: a second identical hostile query in the same process went
    over 1.9 GiB and was killed for nested_replace, recursive_doubling and
    regexp_replace_g, in both languages and under every glibc tunable tried
    (`results/linux-arm64-docker-repeat*.jsonl`). On Linux arm64 repeated hostile
    queries accumulate.
  - `allocator_flush_threshold` / `allocator_bulk_deallocation_flush_threshold` made no
    difference. They act on DuckDB's jemalloc, which is not loaded in either arm64 Linux
    build or on macOS (`duckdb_extensions()` has no `jemalloc` row). Linux x86_64 builds
    do bundle jemalloc; they were not measured.
  - Consequence (inferred from the retained RSS, not run): an absolute-RSS watchdog
    at 512 MB or 1 GB would, after one hostile query on Linux, interrupt every later
    query until the process restarts. The watchdog has to compare against the RSS at
    query start, and retained RSS still grows across queries.
- Node has no way to call `malloc_trim` without a native addon. Python can call it
  through `ctypes`. Neither helped here.

### 3. OS allocation limits

- **macOS: not usable.** `setrlimit(RLIMIT_AS | RLIMIT_DATA)` refuses any soft limit
  below the current virtual size (about 415 GiB for a bare Python process) with EINVAL;
  zsh `ulimit -v` fails the same way. `RLIMIT_AS` is an alias of `RLIMIT_RSS` and is not
  enforced: with it at 1 TiB, a touched 1 GiB allocation succeeds
  (`results/macos-rlimit-probe.txt`, `results/macos-arm64-rlimit.jsonl`).
- **Linux RLIMIT_AS: not usable at a useful size.** At 1024 and 1536 MB Python cannot
  start its watchdog thread ("can't start new thread") and Node fails to `dlopen` the
  DuckDB binding or allocate thread-local storage. Thread stacks and arena reservations
  count against address space, not RSS.
- **Linux RLIMIT_DATA: works in Python, is fatal in Node.**
  - Python: every probe ended with a catchable `duckdb.OutOfMemoryException` ("Failed
    to allocate block of N bytes (bad allocation)") and the process survived. One run
    (recursive_doubling at 1024 MB) left the store unusable: the follow-up normal query
    also hit OOM. The effective ceiling is well below the limit (DuckDB OOMs at 400 to
    520 MB RSS under a 1024 MB limit), because RLIMIT_DATA counts reserved private
    mappings, not resident pages.
  - Node: 3 of 12 runs aborted the process (SIGABRT, V8 "JavaScript heap out of
    memory" or `node::OOMErrorHandler`). Once DuckDB has used the budget, V8 cannot grow
    its own heap, and that is fatal by design.

### 4. Gate-level mitigations (data only)

| mitigation | removes | still gets through (measured) |
|---|---|---|
| (a) deny recursive CTEs | recursive_doubling (1.1 GB) | nested_replace, nested_replace_tiny_literals, damerau_levenshtein, regexp_replace_g (1.2 to 1.9 GB) |
| (b) cap string literal length | nested_replace and regexp_replace_g only if the cap is below about 250 bytes | nested_replace_tiny_literals (longest literal 4 bytes, 1.8 to 1.9 GB), recursive_doubling (literal `'x'`), damerau_levenshtein (repeats a column or short literal with `\|\|`) |
| (c) deny damerau_levenshtein, levenshtein, jaro_winkler and similar | damerau_levenshtein (1.6 GB) | every replace, concat, regexp and recursive shape |
| (d) wrap the agent query to truncate cells in SQL | nothing measured: probes that aggregate (`sum(length(...))`) return one small cell anyway; truncated_wrapper was caught by `max_memory` with or without the wrapper | all aggregate-shaped probes |

Every one closes one class. Exponential growth only needs a short literal and repeated
application, and that can be spread over CTEs, nesting or recursion, so a nesting-depth
cap would also be evaded by a CTE chain. All of (a) to (c) together still leave
nested_replace_tiny_literals at 1.8 to 1.9 GB from 327 bytes of SQL.

### 5. Separate worker process for the store (assessment, not built)

What the measurements say about it:

- A process boundary is the only mechanism measured here that bounds the single-vector
  case. An external sampler with SIGKILL overshot its cap by 60 to 145 MB on macOS
  (2048 MiB cap, peaks 2110 and 2192 MiB) and under 100 MB on Linux (1900 MiB cap,
  peaks 1904 to 1998 MiB), at 20 ms sampling (`results/*-repeat*.jsonl`). On Linux a
  cgroup (`systemd-run --user -p MemoryMax=`), or RLIMIT_DATA inside the worker, enforces
  it in the kernel. An abort inside a worker is acceptable because the parent survives.
- A kill also fixes the retention problem: closing or killing the database is the only
  thing that returned memory on Linux.

Costs:

- On restart everything in the store is gone: every typed table, every envelope row and
  so every handle the agent holds, up to `max_session_bytes` (256 MiB) of retained
  results. Handles would need a "store was reset" error like the stale-handle error.
  Rebuilding would mean keeping a copy of every retained payload in the parent, which
  doubles retained memory.
- IPC: payloads are bounded by `max_payload_bytes` (1 MiB), and the query result is
  bounded by `query_max_bytes` (64 KiB). Copying 1 MiB over a pipe is milliseconds,
  small next to the measured 7.6 s it takes to materialize a 1 MiB dual-channel call
  (comment in `config.py`). Not measured here.
- Python: the store and query modules move behind a small RPC (materialize, query,
  evict, interrupt). `anyio.to_thread` becomes a request to the child; the per-query
  deadline becomes a message, or a kill. The admission semaphore and ordering stay in
  the parent. `multiprocessing` spawn costs about one interpreter start plus the DuckDB
  import per restart.
- TypeScript: `child_process.fork` or a `worker_threads` worker. A worker thread shares
  the process and its RSS, so it does not bound memory; it has to be a child process.
  Values crossing the boundary are `bigint` and `Map`, which structured clone handles
  but JSON does not, so the existing lossless `json.ts` would be the wire format.
- Both: the supervisor needs RSS sampling on macOS (`proc_pid_rusage`, as in
  `memsample.py`, or `ps`) because macOS has no enforced limit; Node would need either a
  native call or `ps` polling.

## Recommendation

1. **Put the DuckDB store in a supervised child process and make its memory cap the
   real bound.** Default worker cap: **2 GiB**, as a new `[limits]` setting, with
   `duckdb_max_memory` kept at 1GB. Reasoning: nothing in-process held a hostile query
   under the cap. The watchdog only bounded recursive growth, rlimits are unavailable
   on macOS and kill Node on Linux, and gate rules each close one class. 2 GiB leaves
   room for the 1 GB `max_memory`, the 256 MiB retention budget and the measured
   overshoot of tracked operators (3x `max_memory` for concat_chain). It sits 4x under
   Slopticus's 8 GiB kill. On breach: kill, restart, fail the query with a clear
   "store reset" error, and make every older handle fail loudly.
   Enforcement: cgroup `MemoryMax` or RLIMIT_DATA in the child where available (Linux),
   parent-side 20 ms RSS/footprint sampling everywhere (needed on macOS). Expect about
   150 MB overshoot.
2. **If the worker is not wanted now, ship the in-process watchdog as a partial
   mitigation, and say it is partial.** Measure against RSS at query start, not an
   absolute value. On breach interrupt; and on Linux, if RSS stays high after the
   query, close and reopen the database. That is the only release that worked, and it
   loses tables exactly like a worker restart. It will not stop single-vector
   amplification, so the 8 GiB host kill stays reachable.
3. **Gate rules are optional defense in depth, not a fix.** Denying recursive CTEs and
   the edit-distance functions removes two measured probes at no cost to normal
   aggregate queries. Literal caps and cell truncation buy nothing measurable.

## What each option does not cover

- Worker process: nothing about memory spent by the parent before the store sees the
  payload (MCP SDK decoding, JSON parse; spec §7 already notes this). On macOS it
  enforces only at sampling granularity. The agent can repeatedly force restarts and so
  erase the session's tables; that is a denial of service to the same agent, but it
  also destroys handles a user may care about.
- In-process watchdog: single-operator amplification (measured up to the natural peak),
  retained RSS on Linux, and accumulation across queries on Linux.
- RLIMIT_DATA: macOS entirely; Node (aborts); one Python run left the store unusable;
  the effective ceiling is far below the configured value.
- RLIMIT_AS: unusable at any limit that means something.
- Gate rules: every class not named, and the tiny-literal nested replace in particular.

## Gaps in this spike

- Linux x86_64 (where DuckDB bundles jemalloc and the allocator flush settings may
  matter) was not measured: the Mac is arm64, and emulating x86_64 was not done.
- Python `duckdb` 1.5.5 (the floor, still in CI) was not measured; only 2.0.1.
- Each cell is one run. Repeats of the macOS baseline across the calibration and sweep
  runs agreed within about 5% (`results/calib.jsonl` vs `results/macos-arm64.jsonl`).
- The watchdog was sampled at 20 ms only; 10 and 50 ms were not compared. Since the
  failures are single-vector overshoot, sampling rate does not change the conclusion.
- `calib.jsonl` holds the first calibration with earlier probe scales and SQL (it
  includes a sort control that turned out to be top-N); the sweeps use the final
  `probes.json`.

## Open questions for the owner

1. Is losing all tables and handles on a cap breach acceptable, or must a breach keep
   the session's earlier results (which needs a parent-side copy and doubles retained
   memory)?
2. Default cap: 2 GiB as proposed, or tie it to `duckdb_max_memory` (for example 2x
   it, minimum 1 GiB)?
3. Should the worker land in both versions at once (the conformance rule says yes), or
   Python first with a one-sided fixture note?
4. Is an interim in-process watchdog worth shipping, given it does not stop the
   shapes that reached 11 to 13 GiB before?
5. Should the cheap gate rules (deny recursive CTEs, deny edit-distance functions) go
   in regardless? Recursive CTEs are currently explicitly supported by the gate.
6. Measure Linux x86_64 and DuckDB 1.5.5 as a follow-up task?

## Follow-up: string-distance growth (2026-11-28)

The owner chose to deny the quadratic string-distance functions (open question 5,
the edit-distance half; recursive CTEs stay allowed). To find every member of the
class, `distance_growth.py` runs each DuckDB string-similarity function on two
strings of growing length, one probe per child process, killed at 1 GiB RSS or
60 s. `delta_mb` is peak RSS during the call minus peak RSS before it; inputs are
at most 8 MB. macOS arm64, `threads=1`. DuckDB 2.0.1 and 1.5.5 expose the same set
of these functions; no built-in macro calls any of them. The table is the 2.0.1 run.
The 1.5.5 rerun was within about 10% but its raw output was not kept; rerun
`spike/memory-cap/distance_growth.py` to reproduce.

| function | lengths | seconds | delta_mb | growth per doubling | decision |
|---|---|---|---|---|---|
| damerau_levenshtein | 2k / 4k / 8k | 0.018 / 0.066 / 0.27 | 32 / 126 / 503 | memory 4x, time 4x | deny |
| levenshtein | 16k / 32k / 64k | 0.36 / 1.42 / 5.71 | 1 / 1 / 2 | time 4x | deny |
| editdist3 (alias of levenshtein) | 16k / 32k / 64k | 0.44 / 1.44 / 5.73 | 1 / 1 / 1 | time 4x | deny |
| jaro_similarity | 128k / 512k / 2M | 0.029 / 0.42 / 6.08 | 9 / 32 / 123 | time 4x (14x per 4x), memory linear | deny |
| jaro_winkler_similarity | 128k / 512k / 2M | 0.029 / 0.41 / 6.05 | 9 / 32 / 123 | time 4x, memory linear | deny |
| hamming | 250k / 1M / 4M | 0.001 / 0.002 / 0.004 | 1 / 1 / 1 | linear | allow |
| mismatches (alias of hamming) | 250k / 1M / 4M | 0.001 / 0.002 / 0.004 | 1 / 1 / 1 | linear | allow |
| jaccard | 250k / 1M / 4M | 0.002 / 0.004 / 0.011 | 1 / 1 / 1 | linear (character sets) | allow |

Times are DuckDB 2.0.1 except editdist3 (1.5.5). The brief was to deny superlinear
time as well as superlinear memory: one pair of 64,000-character strings already
spends 5.7 s of the 10 s default timeout (§6.2) in levenshtein. Whether the timeout
interrupts inside one call was not measured.
`array_*` and `list_*` distance and similarity functions are linear in list length
and stay allowed. Python and Node run the same engine, so the Node gate denies the
same names.
