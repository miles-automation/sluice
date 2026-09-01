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

## Release decision

- Cut v0.1.1 when a verified fix improves compatibility, packaging,
  documentation, or existing behavior.
- Keep the feature freeze when evidence is sparse or contradictory.
- Open a v0.2 proposal only when repeated sessions identify the same missing
  capability and the proposal preserves the security and resource boundaries
  in `spec/001-scratch-db.md`.
