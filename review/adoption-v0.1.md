# v0.1 adoption validation

Sluice is under a feature freeze after the public v0.1.0 release. This cycle
tests the released behavior in real integrations before choosing v0.2 scope.
Only reproducible bugs, compatibility failures, packaging friction, and
documentation gaps belong in v0.1.x.

## Exit criteria

- At least 10 real Sluice-backed sessions.
- At least two MCP clients.
- At least three downstream MCP servers, including one server with paginated
  tool discovery or multi-round-trip behavior.
- Successful install and startup coverage on Linux, macOS, and Windows.
- Every observed failure has a minimal reproduction or is explicitly recorded
  as inconclusive.
- No v0.2 feature is selected without evidence from these sessions.

## Automated baseline

CI runs the full suite, contract checks, typing, lint, package build, and an
isolated wheel smoke test on Linux. The built wheel is then installed and its
import, version metadata, typing marker, and CLI entry point are smoke-tested
on macOS and Windows.

## Session record

Add one row for each real session. Do not record credentials, payload contents,
or client conversation text.

| Date | Client/version | Downstream/version | OS | Install/start | Passthrough | Materialize/query | Finding |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-01 | Claude Code 2.1.220 / Sonnet 5 | Everything 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: structured result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Everything 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: structured result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Everything 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: structured result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Everything 2026.8.31 | macOS 26.6.2 arm64 | Pass | Pass: image result unchanged | Not applicable | None |
| 2026-09-01 | MCP Python 2.1.1 | Filesystem 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: directory result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Filesystem 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: directory result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Filesystem 2026.8.31 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: directory result, exact count 1 | None |
| 2026-09-01 | MCP Python 2.1.1 | Time 2026.8.18 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: current time, exact count 1 | Downstream logs validation warnings for `server/discover`; Sluice falls back to MCP 2025-11-25 |
| 2026-09-01 | MCP Python 2.1.1 | Time 2026.8.18 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: current time, exact count 1 | Same fallback; no functional failure |
| 2026-09-01 | MCP Python 2.1.1 | Time 2026.8.18 | macOS 26.6.2 arm64 | Pass | Not exercised | Pass: time conversion, exact count 1 | Same fallback; no functional failure |

All sessions used a fresh install of `mcp-sluice==0.1.0` from public PyPI.
They covered nine successful materialization/query paths and one binary
passthrough path. The Everything server supplied a current reference server
with multi-round-trip behavior in its catalog; these sessions did not invoke
an interactive tool because the negotiated downstream protocol was
2025-11-25.

## Result

The cycle met its measurable exit criteria: 10 sessions, two clients, three
downstream servers, and Linux/macOS/Windows installation coverage all passed.
No Sluice defect or release-blocking documentation problem was reproduced, so
there is no evidence for a v0.1.1 release or a v0.2 feature yet. Keep the
feature freeze and collect ordinary user feedback against v0.1.0.

## Release decision

- Cut v0.1.1 when a verified fix improves compatibility, packaging,
  documentation, or existing behavior.
- Keep the feature freeze when evidence is sparse or contradictory.
- Open a v0.2 proposal only when repeated sessions identify the same missing
  capability and the proposal preserves the security and resource boundaries
  in `spec/001-scratch-db.md`.
