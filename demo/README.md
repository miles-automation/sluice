# The M5 demo

`plan/001-scratch-db.md` §"Not in CI" and Definition of Done item 5: an agent
asked for the median of a `score` column over the same 400 deterministic rows
gets it wrong reading the raw payload and right reading it through Sluice's
`query` tool. This is a live-model comparison, not a test — it is
**deliberately not run in CI** because gating CI on model behavior makes the
suite lie.

Run it with:

```bash
uv run python -m demo.median
```

Or, to see the configs and the mechanically-computed expected answer without
spending a model call:

```bash
uv run python -m demo.median --dry-run
```

## What it does

Two conditions, one process each, same prompt, same `rows(400)` payload from
`tests/fake_server/server.py::rows_payload`:

- **baseline** — the `claude` CLI (Claude Code) is pointed at `tests.fake_server`
  directly over stdio via a temporary `--mcp-config`. The agent gets the raw
  400-row JSON array back from the tool call and has to compute the median
  itself, from a payload it can see in full.
- **treatment** — the `claude` CLI is pointed at Sluice instead, via a second
  temporary `--mcp-config` plus a temporary `sluice.toml` that proxies the
  *same* fake server. The agent gets a short preview (3 rows, per
  `preview_rows`) and a table handle, plus a `query` tool. If it runs SQL
  through `query`, DuckDB computes the median exactly; the model never has to
  eyeball 400 numbers.

The expected median is computed in-process from `rows_payload(n)` with
`statistics.median` — the same function the fake server calls, not a
hand-copied constant — so the harness can't drift out of sync with the data
it's grading against.

Each run writes `demo/transcripts/<UTC-timestamp>/`:

- `baseline.txt`, `treatment.txt` — scrubbed raw stdout+stderr from each
  `claude` invocation.
- `report.json` — model name, prompt, expected median, tolerance, per-condition
  status/exit-code/parsed-answer/correctness, and the non-determinism note
  below, verbatim.

## Non-determinism

This calls a live model. `claude` can choose a different tool, retry
differently, or phrase its final numeric answer differently between two runs
with an identical prompt, and CLI or model updates change behavior out from
under a pinned config. **A single green run is evidence, not a guarantee.**
The script never asserts an outcome — it records `parsed_answer` and
`correct` for whoever reads the report, and prints the caveat above on every
run. Treat `demo/` the way `plan/001-scratch-db.md` does: separate from the
test suite, not a CI gate.

## Never fabricated

If `claude` can't be invoked — not on `PATH`, blocked by a permission gate in
a non-interactive/sandboxed session with no approver available, a timeout, a
non-zero exit before an answer came back, or an `--output-format stream-json`
payload that doesn't parse the way this script expects — the affected
condition is written to `report.json` with `"status": "pending"` and the raw
reason, never a guessed or backfilled answer. If you see `"pending"` in a
report, the comparison **was not observed** for that condition; don't read
`"correct": null` as a failure.

This is not hypothetical: this script was authored and validated inside a
non-interactive session where the harness (`uv`, `pytest`, `ruff`, `mypy`,
and the `claude` CLI itself) was denied at the permission layer with no human
available to approve it. The transcripts committed alongside this file (if
any) were **not** produced by that session; check `report.json`'s
`generated_at_utc` and each condition's `status` before trusting a result.

## Prerequisites

- The `claude` CLI (Claude Code) installed and already authenticated,
  independent of this repo — this script never reads, sets, or forwards a
  credential.
- `uv sync` has installed this project's own dependency group, so
  `tests.fake_server` and `sluice.naming` import cleanly.

## Flags this script assumes

`--mcp-config`, `--strict-mcp-config`, `--tools`, `--allowedTools`,
`--permission-mode`, `--max-turns`, `--output-format stream-json`, and `--model`
are assumed to exist on
the installed `claude` CLI. CLI flags move between releases; if a condition
comes back `"pending"` with a `reason` mentioning an unrecognized option, check
`claude -p --help` against the flags built in `demo/median.py::_run_condition`
and adjust there. The script always writes the exact `argv` it invoked into
`report.json` so a mismatch is diagnosable from the report alone.

## Safety notes

- Every config is written under `tempfile.TemporaryDirectory` (mode `0700`)
  and deleted when the run ends.
- The fake downstream server takes no credential, so nothing secret ever
  enters either config.
- `--tools` and `--allowedTools` are scoped to `ToolSearch` (needed while the
  proxied MCP server finishes connecting) plus the exact data tool(s) each
  condition needs (`mcp__fake__rows` for baseline; the Sluice-mounted `rows`
  tool plus `mcp__sluice__query` for treatment). The agent cannot reach for
  shell, file, browser, or other general-purpose tools.
- Transcripts are scrubbed for credential-shaped substrings before being
  written to disk, as defense in depth — the primary guarantee is that
  nothing secret is ever passed to `claude` in the first place.
