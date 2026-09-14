# Spec 002: Bounded pagination

Status: implemented on branch `feature/622` (Spark Swarm task 622). Extends spec 001; nothing in
spec 001 changes. Motivated by the 2026-09-13 comparison recorded in the platform workspace under
`research/sluice-bof-comparison/RESULTS.md`, where an agent fetching 19 pages of one tool spent 19 of
its 22 turns on the loop and then hand-wrote a 19-table `UNION ALL`.

## 1. What it is

An explicitly configured operation that fetches successive pages of **one** downstream tool and records
every row in **one** table. It is exposed as a second tool next to the original, named
`<server>__<tool>__all__<tag>` (§3), and the original single-page tool stays mounted unchanged.

It is built on one pagination contract, the offset contract BullshitOrFit uses:

- request: `limit` and `offset` arguments;
- response: a JSON object with an `items` array, a boolean `has_more`, and an integer `next_offset`
  when `has_more` is true.

Every key name is configurable per tool (§2). Nothing is inferred: a tool paginates automatically only
when the operator lists it, and Sluice never guesses the contract of an arbitrary tool.

## 2. Configuration

```toml
[pagination.list_jobs]      # keyed by the downstream tool name
items = "items"             # key holding the page's rows
limit_arg = "limit"         # request argument Sluice supplies
offset_arg = "offset"       # request argument Sluice supplies; the agent may pass a starting value
has_more = "has_more"       # boolean response key
next_offset = "next_offset" # integer response key, read only when has_more is true
page_size = 200             # value sent as limit_arg on every page
max_pages = 100
max_bytes = 8388608         # cumulative bytes of page payloads (both channels count, spec 001 §5.1)
max_seconds = 120
```

Validation happens where the dataclass is constructed. Unknown keys, non-integers, values below 1, a
non-positive or non-finite `max_seconds`, identical `limit_arg` and `offset_arg`, or a `max_bytes`
above `[limits].max_session_bytes` are configuration errors.

## 3. Startup

- **PG-1** Each `[pagination.<tool>]` entry must name a tool the downstream lists. Otherwise Sluice
  fails to start (FR-6 applies).
- **PG-2** The downstream tool must carry `annotations.readOnlyHint = true`. Otherwise Sluice refuses
  to start and says so. Explicit configuration **and** a read-only annotation are both required:
  automatic fetching is a multiplier on whatever the tool does, and an operator's config entry cannot
  vouch for a tool the server itself does not describe as read-only.
- **PG-3** The collection tool is a clone of the downstream tool (FR-3 applies) with four changes:
  `name` per §1 (the tag is taken over the server/tool identity plus the suffix, so it cannot collide
  with the mounted single-page name), `description` (original text, then a note naming the tool and
  the two managed arguments, then the handle note), `inputSchema` (the `limit_arg` property and its
  `required` entry removed; `offset_arg` is kept because it is the resume point), and `outputSchema`
  removed (spec 001 §4.3). Annotations survive, so the read-only hint is visible upstream.
- **PG-4** A collection name that equals any mounted name is a startup failure, like any collision.

## 4. Fetching

- **PG-5** Arguments other than `limit_arg` and `offset_arg` are forwarded unchanged on every page. A
  supplied `limit_arg` is rejected with a tool error naming the managed page size. A supplied
  `offset_arg` must be a non-negative integer and is the first offset fetched; otherwise the fetch
  starts at 0.
- **PG-6** Pages are fetched sequentially. After each page: append `items`; if `has_more` is false the
  fetch is **complete**. If `has_more` is true, `next_offset` must be an integer greater than the
  current offset, not previously seen, and the page must have carried at least one item; otherwise the
  fetch stops as **partial** with reason `missing_next_offset` or `offset_not_advancing`. A page whose
  payload is not a JSON object, whose `items` is not an array, or whose `has_more` is not a boolean
  stops the fetch as partial with reason `malformed_page`.
- **PG-7** Limits: `max_pages` (checked before each fetch), `max_bytes` (checked after each page;
  the page that crossed the ceiling is kept), and `max_seconds` as a wall clock over the whole loop
  (a page call in flight when the clock expires is cancelled). Each stops the fetch as partial with
  reason `max_pages`, `max_bytes` or `max_seconds`.
- **PG-8** A page that returns `isError`, raises a downstream failure (spec 001 §8), or returns an
  `InputRequiredResult` stops the fetch as partial with reason `page_error`, `page_failed` or
  `interactive`. Sluice never answers an elicitation on the agent's behalf (spec 001 §11), so an
  interactive page cannot be paginated through.
- **PG-9** Every partial result records the `resume_offset`: the first offset that was **not**
  fetched, when resuming is safe (`max_pages`, `max_bytes`, `max_seconds`, `page_error`,
  `page_failed`, `interactive`, `malformed_page`); `null` when it is not (`missing_next_offset`,
  `offset_not_advancing`), because the server's own cursor cannot be trusted there.

## 5. Recording and the handle

- **PG-10** If at least one page was fetched, the collected rows are materialized as a single payload
  `{"items": [...]}` through the ordinary pipeline (spec 001 §5), producing one envelope row and one
  table named `<server>__<tool>__all__<tag>__<scope>__<seq>`. The envelope's `tool` is the original
  tool name; its `args` are the agent's arguments plus a `sluice_pagination` object equal to the
  summary below. The collection is admitted up to twice `max_bytes` rather than
  `[limits].max_payload_bytes`, because `max_bytes` is the operator's explicit bound on this operation
  and re-serialization of the combined rows can exceed the sum of the page payloads.
- **PG-11** The handle carries a pagination line and a `pagination` object in structured content:
  `status` (`complete` or `partial`), `reason`, `detail`, `pages`, `rows`, `bytes`, `seconds`,
  `resume_offset`. A partial handle says in prose that rows from `resume_offset` onward were not
  fetched and how to continue, or that the remainder cannot be resumed safely.
- **PG-12** If no page was fetched, the result is an error, never an empty table. A downstream
  `isError` page is returned with its content intact plus a Sluice note; other reasons produce a
  Sluice error result. The envelope records the call with `failure_class` set to the reason.

## 6. What this does not do

No cursor-token pagination, no `Link` headers, no inference of a contract from a tool's schema, no
parallel page fetches, no cross-call merging of a resumed fetch into the earlier table (a resumed
fetch is a new call with its own table; joining the two is a `UNION ALL` the agent writes). Each is a
choice for a later revision once a real downstream needs it.
