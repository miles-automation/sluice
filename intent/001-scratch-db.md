# Intent 001: Scratch DB for MCP tool results

Status: accepted
Date: 2026-08-28
Author: Rich Miles (intake by Claude)

## Problem

An agent calling MCP tools pays twice for every large result.

It pays in **context**. A tool result enters the conversation and is then re-sent
on every subsequent turn of the agent loop. A 90 KB JSON payload fetched on turn
3 is still being re-transmitted on turn 30. The cost is not the one-time read, it
is the repeated carry.

It pays in **accuracy**. Models are unreliable at aggregation over many records
held as text. Counting, summing, deduplicating, and computing order statistics
across a few hundred objects in a payload produces answers that are usually close
and occasionally wrong, with no signal distinguishing the two cases. A median over
400 rows read out of a payload is a coin flip. The same median computed by a
database is exact, every time.

Both problems have the same shape: the payload is being used as a data structure
while living in a medium that is neither addressable nor computable.

## Proposed outcome

Sluice: a passthrough MCP server that sits between the agent and one or more
downstream MCP servers, mounts the union of their tools, and proxies calls
through. That part is transparent to the agent.

The addition is that every tool result is materialized into a session-scoped
DuckDB database before being returned. What the agent gets back is not the
payload but a **handle**: a short preview, the name of a table, its row count,
and its column list. Sluice exposes one tool of its own, `query`, which runs
read-only SQL against those tables.

The large payload is written to a table and never enters the context window, so
it is never re-sent. Aggregation becomes a SQL statement over real columns with
real types, which is deterministic.

## Affected users and systems

**The agent (primary).** Its tool-calling behavior changes in one specific way:
results come back as handles, and it must issue a follow-up `query` to do
anything analytical with them. Everything else about calling the tool is
unchanged, including the tool's own name, arguments, and schema.

**The MCP client.** Sees a single MCP server exposing namespaced tools plus
`query`. It does not know Sluice is a proxy. Sluice speaks stdio in v0, so the
client's existing stdio server configuration is the whole integration.

**Downstream MCP servers.** Unaware of Sluice. They receive ordinary tool calls.
Sluice holds one client session per downstream server for the life of the
process.

**The operator (Rich).** Configures which downstream server to front, in a TOML
file. No deployment, no service, no account.

## Constraints

- Python 3.14 or newer. `requires-python = ">=3.14"`. No compatibility shims for
  older versions. PEP 604 unions, PEP 585 builtin generics, `type` statements.
  No `typing.Optional`, no `typing.List`, no `TypeAlias`.
- The official MCP Python SDK. Not a hand-rolled protocol implementation.
- DuckDB as the engine. In-process, no server, zero-configuration typed columns
  and good JSON inference.
- Type hints throughout, pytest for tests. Idiomatic modern Python of the kind
  written in a FastAPI and Postgres shop.
- The database is in-memory and dies with the process.

## Decisions taken during intake

These were open at the start of intake and are now settled. They are recorded
here because the spec depends on them and a reader of the spec alone would
otherwise think they were arbitrary.

1. **Depth-1 flattening.** Top-level scalar fields become typed columns. Nested
   objects and arrays become JSON columns. Rejected alternative: full DuckDB
   STRUCT and LIST inference, which is more expressive but prints a large schema
   into the handle, which is the exact context cost the project exists to avoid.
2. **The handle rides in `content`.** Mirrored into `structuredContent` for
   clients that use it. Rejected alternative: `_meta`, which clients do not feed
   to the model, so the agent would receive a truncated payload with no way to
   learn the table name.
3. **Always intercept.** Every proxied result produces a handle and a table.
   Rejected alternative: a size gate passing small results through untouched.
   Rich chose uniformity of contract over per-call efficiency. The efficiency
   concern is answered by the preview budget rule in the spec (a payload smaller
   than the preview budget is reproduced in the preview in full), not by a gate.
4. **No separate fetch tool.** The agent recovers a full payload by selecting it
   out of the envelope table with `query`.
5. **Errors and binary content pass through verbatim.** A tool error is
   diagnostic and mangling it makes debugging worse. Image and audio content has
   nothing to flatten. Both are still recorded in the envelope.
6. **Proxied tool descriptions get one appended sentence** explaining the handle
   contract. The upstream description is otherwise preserved verbatim.
7. **Per-conversation scope isolation is in v0**, decided after review. A client
   may reuse one stdio process across conversations, so "the DB dies with the
   session" was really "dies with the process." Worse, after a restart, sequence
   numbers restart and a resumed conversation's stale handle could name a table
   that exists and holds different data. Scope tags in table names fix that
   unconditionally; the isolation half is capability-based rather than enforced,
   and spec §12 states the residual risk. The cost is table discovery: the
   enumeration that discovery needs is exactly what isolation must block.
8. **Multi-round-trip tool calls are relayed end to end**, decided after review.
   The hazard otherwise is not that interactive tools break, it is that Sluice
   answers elicitation and sampling prompts addressed to the real client and its
   human.
9. **The reference downstream server is a purpose-built fake, in-repo.** It emits
   controlled payloads (400 homogeneous rows, heterogeneous rows, deeply nested,
   non-tabular, oversized) so CI is hermetic and the correctness property test
   has ground truth.

## Non-goals for v0

Written down as choices, not oversights.

- **No cross-session persistence.** The DB dies with the session. Persistence
  would immediately raise questions of storage location, retention, size limits,
  cleanup, and, because tool results contain whatever the downstream server had
  access to, data governance. Every one of those is a real question and none of
  them is the question this project is testing. The value proposition being
  tested is "SQL over the current session's tool results", and an in-memory DB
  tests it completely.
- **No cross-server joins or entity resolution.** Joining a GitHub issue table to
  a Linear ticket table needs identity mapping, which is a product in itself.
- **No auth, policy enforcement, redaction, or audit layer.** A proxy that sees
  every tool call and every result is the natural place to put all four, and that
  is plausibly the eventual product. It is not this version. The envelope table
  is the seed of an audit log, and that is as far as v0 goes.

  One exception, added after review: **scope isolation** (spec §12). It is not
  really an auth layer, it is a correctness fix, because without it a resumed
  conversation can get a clean answer about a different result set. Even so, it is
  capability-based rather than enforced, and enforced isolation stays in v1.
- **No hosted service, no UI.** Local stdio process only.
- **No table discovery.** Direct consequence of scope isolation: enumeration is
  what has to be blocked, so the agent's own handles in its transcript are the
  index of what exists.

## Open questions

1. **Python floor.** 3.15 releases 2026-10-01, roughly six weeks out. Moving the
   floor to 3.15 is deliberately left open rather than decided. The gating factor
   is DuckDB wheel availability for 3.15: DuckDB ships compiled extension
   modules, so a source build is not an acceptable fallback for a tool meant to
   be installed with one command. Revisit once DuckDB publishes cp315 wheels.
2. **Multiple downstream servers.** Scope says one. Namespacing exists precisely
   because more than one is coming. The config format should admit several from
   day one even though v0 refuses to start with more than one configured.
3. **Whether the agent needs table discovery beyond the handles in context.** If
   a handle scrolls out of context, is `SELECT * FROM sluice_schema` enough, or
   does discovery need to be more prominent? Deferred until observed in use.
4. **What happens on repeated calls to the same tool.** v0 gives each call its
   own table plus a `__latest` view. Whether the agent wants a union across calls
   to the same tool is a real question that only shows up under use.

## How we will know it worked

The correctness criterion is a property: **an aggregate computed via `query` must
equal the same aggregate computed directly over the raw source data.** This is
mechanically checkable and belongs in CI.

"Equal" is bounded by a domain and splits in two inside it, both measured rather
than assumed. Integer columns within ±2^53 can claim exactness; larger integer
columns and every floating-point column are flagged non-exact. A column-level
flag cannot encode operation-dependent floating-point error: even
`[1e6, 1e-10, -1e6]` misses both stated tolerances for `avg` and `sum`.
Therefore `count`, `min`, `max`, `median`, `count(DISTINCT)`, `GROUP BY` counts,
and integer `sum` are the exactness property in v0. `avg` and float `sum` remain
bounded regression measurements using relative tolerance `1e-9` and absolute
tolerance `1e-12`; they are not guarantees attached to an exact column.
Per-aggregate exactness metadata is future design work. A criterion demanding
universal floating-point equality would be false on arrival:
`avg([-1e308, 1, 2, 1e308])` is `0.0` in DuckDB and `0.75` in Python.

The demonstration is separate and is not a test: an agent asked for a median over
roughly 400 rows gets it wrong reading the payload and right reading the table.
That is an eval against a model, it is non-deterministic, and it must not gate CI.
