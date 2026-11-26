import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { Client, Tool } from "@modelcontextprotocol/client";
import { ConfigError, paginationConfig, type PaginationConfig } from "../src/config.ts";
import { Interceptor } from "../src/intercept.ts";
import { makeLimits } from "../src/limits.ts";
import type { PaginationSummary, ToolResult } from "../src/models.ts";
import { collectionName, mountedName, quoteIdent } from "../src/naming.ts";
import { StopReason, fetchAll, type FetchOutcome, type PageCall } from "../src/paginate.ts";
import { parseJson, type JsonObject } from "../src/json.ts";
import { Proxy, exposeCollection } from "../src/proxy.ts";
import { collectionResult, runCollection } from "../src/server.ts";
import { Store } from "../src/store.ts";
import { rowsPayload } from "./fixtures/fake-server.ts";
import { fakeConfig, sqlRows, structured, tableName, tables, text, withSluice } from "./support.ts";

const PAGED = collectionName("fake", "paged");

function paged(tool: string, overrides: Readonly<Record<string, unknown>> = {}): PaginationConfig {
  return paginationConfig(tool, { page_size: 100, ...overrides });
}

function pagination(result: { readonly structuredContent?: unknown }): Record<string, unknown> {
  return structured(result)["pagination"] as Record<string, unknown>;
}

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<Awaited<ReturnType<Client["callTool"]>>> {
  return client.callTool({ name, arguments: args });
}

async function sql(client: Client, query: string): Promise<string> {
  return text(await client.callTool({ name: "query", arguments: { sql: query } }));
}

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? (sorted[middle] ?? 0) : ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2;
}

describe("pagination: startup", () => {
  test("startup refuses a tool the downstream did not list", async () => {
    await assert.rejects(Proxy.start(fakeConfig({ pagination: [paged("absent")] })), (error: unknown) => {
      assert.ok(error instanceof ConfigError);
      assert.match(error.message, /did not list/u);
      return true;
    });
  });

  test("startup refuses a tool without a read-only annotation", async () => {
    await assert.rejects(Proxy.start(fakeConfig({ pagination: [paged("paged_mutable")] })), (error: unknown) => {
      assert.ok(error instanceof ConfigError);
      assert.match(error.message, /readOnlyHint/u);
      return true;
    });
  });

  test("the collection is listed next to the original", () =>
    withSluice(fakeConfig({ pagination: [paginationConfig("paged")] }), async ({ client }) => {
      const listing = await client.listTools();
      const names = new Set(listing.tools.map((tool) => tool.name));
      assert.ok(names.has(mountedName("fake", "paged")));
      const collection = listing.tools.find((tool) => tool.name === PAGED);
      assert.ok(collection !== undefined);
      const properties = collection.inputSchema.properties ?? {};
      assert.ok(!("limit" in properties));
      assert.ok("offset" in properties);
      assert.deepEqual(collection.inputSchema.required, []);
      assert.equal(collection.annotations?.readOnlyHint, true);
      assert.equal(collection.outputSchema, undefined);
      assert.ok(collection.description?.includes("every page of `paged`"));
      const single = listing.tools.find((tool) => tool.name === mountedName("fake", "paged"));
      assert.ok(single?.description?.includes(`call \`${PAGED}\` instead`));
      const untouched = listing.tools.find((tool) => tool.name === mountedName("fake", "rows"));
      assert.ok(!untouched?.description?.includes("instead"));
    }));
});

describe("pagination: through the product path", () => {
  test("a complete fetch lands every row in one table", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450 });
      assert.notEqual(result.isError, true);
      assert.ok(text(result).includes("pagination: complete  pages=5  rows=450"));
      assert.equal(pagination(result)["status"], "complete");
      assert.equal(pagination(result)["resume_offset"], null);
      const table = tableName(result);
      assert.ok(table.includes("__all__"));
      assert.equal(tables(result)[0]?.["row_count"], 450);
      assert.ok((await sql(client, `SELECT count(*) FROM "${table}"`)).includes("| 450 |"));
      const expected = median(rowsPayload(450).map((row) => row.score));
      assert.ok((await sql(client, `SELECT median(score) AS m FROM "${table}"`)).includes(`| ${expected} |`));
      const grouped = await sql(client, `SELECT tag, count(*) AS n FROM "${table}" GROUP BY tag ORDER BY tag`);
      const counts = new Map<string, number>();
      for (const row of rowsPayload(450)) {
        counts.set(row.tag, (counts.get(row.tag) ?? 0) + 1);
      }
      for (const [tag, n] of [...counts].sort()) {
        assert.ok(grouped.includes(`| ${tag} | ${n} |`), grouped);
      }
    }));

  test("a single page is still a collection", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 7 });
      assert.equal(pagination(result)["pages"], 1);
      assert.equal(tables(result)[0]?.["row_count"], 7);
    }));

  test("a fetch resumes from an offset", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450, offset: 300 });
      const ids = await sql(client, `SELECT min(id) AS lo, max(id) AS hi, count(*) AS n FROM "${tableName(result)}"`);
      assert.equal(pagination(result)["pages"], 2);
      assert.ok(ids.includes("| 300 | 449 | 150 |"), ids);
    }));

  test("the envelope records the collection call", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 250 });
      const envelope = String(structured(result)["envelope_table"]);
      const callId = String(structured(result)["call_id"]);
      const row = await sql(client, `SELECT tool, args FROM ${quoteIdent(envelope)} WHERE call_id = '${callId}'`);
      assert.ok(row.includes("| paged |"));
      assert.ok(row.includes("sluice_pagination"));
    }));

  test("max_pages yields a partial result with a resume offset", () =>
    withSluice(fakeConfig({ pagination: [paged("paged", { max_pages: 2 })] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450 });
      const count = await sql(client, `SELECT count(*) FROM "${tableName(result)}"`);
      assert.ok(text(result).includes("pagination: PARTIAL, stopped by max_pages"));
      assert.ok(text(result).includes("call again with offset=200"));
      const summary = pagination(result);
      assert.deepEqual(
        { status: summary["status"], reason: summary["reason"], pages: summary["pages"], rows: summary["rows"], resume_offset: summary["resume_offset"] },
        { status: "partial", reason: "max_pages", pages: 2, rows: 200, resume_offset: 200 },
      );
      assert.ok(count.includes("| 200 |"));
    }));

  test("max_bytes never keeps the page that would cross it", () =>
    withSluice(fakeConfig({ pagination: [paged("paged", { max_bytes: 10_000 })] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450 });
      const count = await sql(client, `SELECT count(*) FROM "${tableName(result)}"`);
      const summary = pagination(result);
      assert.equal(summary["status"], "partial");
      assert.equal(summary["reason"], "max_bytes");
      assert.ok(Number(summary["bytes"]) <= 10_000);
      assert.ok(Number(summary["pages"]) >= 1 && Number(summary["pages"]) < 5);
      assert.equal(summary["resume_offset"], summary["rows"]);
      assert.ok(text(result).includes("was not kept"));
      assert.ok(count.includes(`| ${String(summary["rows"])} |`));
    }));

  test("a first page over max_bytes is an error with status", () =>
    withSluice(fakeConfig({ pagination: [paged("paged", { max_bytes: 100 })] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450 });
      assert.equal(result.isError, true);
      assert.equal(result.structuredContent, undefined);
      assert.ok(text(result).includes("fetched no pages (max_bytes)"));
      assert.ok(text(result).includes("ceiling 100"));
    }));

  test("a page without has_more is malformed, not final", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_no_has_more")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_no_has_more"), { n: 450 });
      assert.equal(result.isError, true);
      assert.equal(result.structuredContent, undefined);
      assert.ok(text(result).includes("fetched no pages (malformed_page)"));
      assert.ok(text(result).includes("`has_more` is missing"));
    }));

  test("max_seconds stops the fetch and leaves the proxy usable", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_slow", { max_seconds: 0.5 })] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_slow"), { n: 450, delay_ms: 300 });
      const summary = pagination(result);
      const after = await call(client, mountedName("fake", "rows"), { n: 3 });
      assert.equal(summary["status"], "partial");
      assert.equal(summary["reason"], "max_seconds");
      assert.ok(Number(summary["pages"]) >= 1 && Number(summary["pages"]) < 5);
      assert.equal(summary["resume_offset"], summary["rows"]);
      assert.notEqual(after.isError, true);
    }));

  test("cancelling a collection call stops the fetch without recording it", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_slow")] }), async ({ client, store }) => {
      const controller = new AbortController();
      setTimeout(() => controller.abort(), 400);
      await assert.rejects(
        client.callTool({ name: collectionName("fake", "paged_slow"), arguments: { n: 450, delay_ms: 300 } }, { signal: controller.signal }),
      );
      await new Promise((resolve) => setTimeout(resolve, 2000));
      assert.deepEqual(await sqlRows(store, "SELECT count(*)::INTEGER FROM sluice_calls"), [[0]]);
      const after = await call(client, mountedName("fake", "rows"), { n: 3 });
      assert.notEqual(after.isError, true);
    }));

  test("a non-advancing offset is a partial result", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_stuck")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_stuck"), { n: 450 });
      const summary = pagination(result);
      assert.equal(summary["reason"], "offset_not_advancing");
      assert.equal(summary["pages"], 1);
      assert.equal(summary["rows"], 100);
      assert.equal(summary["resume_offset"], null);
      assert.ok(text(result).includes("cannot be resumed safely"));
    }));

  test("a missing next_offset is a partial result", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_no_next")] }), async ({ client }) => {
      const summary = pagination(await call(client, collectionName("fake", "paged_no_next"), { n: 450 }));
      assert.equal(summary["reason"], "missing_next_offset");
      assert.equal(summary["pages"], 1);
      assert.equal(summary["resume_offset"], null);
    }));

  test("a failed later page keeps the pages before it", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_error_at")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_error_at"), { n: 450 });
      const count = await sql(client, `SELECT count(*) FROM "${tableName(result)}"`);
      assert.notEqual(result.isError, true);
      const summary = pagination(result);
      assert.equal(summary["reason"], "page_error");
      assert.equal(summary["detail"], "page unavailable");
      assert.equal(summary["pages"], 2);
      assert.equal(summary["resume_offset"], 200);
      assert.ok(count.includes("| 200 |"));
    }));

  test("a failed first page is an error, not an empty table", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_error_at")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_error_at"), { n: 450, offset: 200 });
      assert.equal(result.isError, true);
      const texts = result.content.flatMap((block) => (block.type === "text" ? [block.text] : []));
      assert.equal(texts[0], "page unavailable");
      assert.ok(texts.at(-1)?.includes("fetched no pages (page_error)"));
      assert.equal(result.structuredContent, undefined);
    }));

  test("a malformed page is a partial result", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_bad_items")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_bad_items"), { n: 450 });
      assert.equal(result.isError, true);
      assert.ok(text(result).includes("malformed_page"));
      assert.ok(text(result).includes("not an array"));
    }));

  test("an interactive page stops the fetch", () =>
    withSluice(
      fakeConfig({ pagination: [paged("paged_interactive")] }),
      async ({ client }) => {
        const summary = pagination(await call(client, collectionName("fake", "paged_interactive"), { n: 450 }));
        assert.equal(summary["reason"], "interactive");
        assert.equal(summary["pages"], 1);
        assert.equal(summary["resume_offset"], 100);
      },
      { capabilities: { elicitation: { form: {} } } },
    ));

  test("an agent-supplied limit is rejected", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, PAGED, { n: 450, limit: 5 });
      assert.equal(result.isError, true);
      assert.ok(text(result).includes("managed by Sluice"));
    }));

  test("the original tool still returns one page", () =>
    withSluice(fakeConfig({ pagination: [paged("paged")] }), async ({ client }) => {
      const result = await call(client, mountedName("fake", "paged"), { n: 450, limit: 30, offset: 0 });
      assert.equal(structured(result)["pagination"], null);
      assert.equal(tables(result)[0]?.["row_count"], 30);
    }));

  test("collected rows keep integers past 2^53 exactly", () =>
    withSluice(fakeConfig({ pagination: [paged("paged_lossless")] }), async ({ client }) => {
      const result = await call(client, collectionName("fake", "paged_lossless"), {});
      assert.equal(pagination(result)["pages"], 2);
      const values = await sql(client, `SELECT big::VARCHAR AS big, typeof(f) AS t, "at" FROM "${tableName(result)}" ORDER BY "at"`);
      assert.ok(values.includes("| 9223372036854775807 | DOUBLE | 0 |"), values);
      assert.ok(values.includes("| 9223372036854775807 | DOUBLE | 1 |"), values);
    }));
});

function partialSummary(): PaginationSummary {
  return {
    status: "partial",
    reason: "max_pages",
    detail: "2 pages fetched",
    pages: 2,
    rows: 200,
    bytes: 12_000,
    seconds: 0.1,
    resumeOffset: 200n,
  };
}

describe("pagination: fallback paths keep the status", () => {
  test("an oversize passthrough still carries the pagination status", async () => {
    const limits = makeLimits();
    const store = await Store.open(limits);
    try {
      const interceptor = new Interceptor(store, limits, { logger: () => undefined });
      const items = Array.from({ length: 50 }, (_, i) => ({ i }));
      const result = await interceptor.intercept({
        server: "fake",
        tool: "paged",
        mounted: PAGED,
        arguments: {},
        result: { content: [], structuredContent: { items } },
        meta: null,
        startedAt: new Date(),
        payloadCeiling: 10,
        pagination: partialSummary(),
      });
      const texts = result.content.flatMap((block) => (typeof block.text === "string" ? [block.text] : []));
      assert.ok(texts.some((line) => line.includes("pagination: PARTIAL, stopped by max_pages")));
      assert.ok(texts.some((line) => line.includes("call again with offset=200")));
    } finally {
      store.close();
    }
  });

  test("collectionResult turns a raw passthrough into an error with status", () => {
    const raw: ToolResult = {
      content: [{ type: "text", text: "sluice: result passed through unmodified." }],
      structuredContent: { items: [{ i: 1 }] },
    };
    const result = collectionResult(raw, partialSummary());
    assert.equal(result.isError, true);
    assert.deepEqual(result.structuredContent, {
      pagination: {
        status: "partial",
        reason: "max_pages",
        detail: "2 pages fetched",
        pages: 2,
        rows: 200,
        bytes: 12_000,
        seconds: 0.1,
        resume_offset: 200,
      },
      tables: [],
    });
    assert.ok(text(result).includes("no rows are returned"));
    assert.ok(text(result).includes("passed through unmodified"));
  });

  test("collectionResult keeps a recorded handle", () => {
    const recorded: ToolResult = {
      content: [{ type: "text", text: "handle" }],
      structuredContent: { pagination: { status: "complete" }, tables: [{ name: "t" }] },
    };
    assert.equal(collectionResult(recorded, partialSummary()), recorded);
  });

  test("concurrent collections respect admission", async () => {
    const limits = makeLimits({ max_concurrent_materializations: 2 });
    const store = await Store.open(limits);
    try {
      const interceptor = new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined });
      let inFlight = 0;
      let peak = 0;
      const source = {
        async fetchCollection(): Promise<FetchOutcome> {
          inFlight += 1;
          peak = Math.max(peak, inFlight);
          try {
            await new Promise((resolve) => setTimeout(resolve, 200));
          } finally {
            inFlight -= 1;
          }
          return {
            status: "complete",
            reason: StopReason.COMPLETE,
            detail: null,
            pages: 1,
            bytes: 100,
            seconds: 0.2,
            resumeOffset: null,
            rows: [new Map([["i", 1n]])],
            firstPageError: null,
          };
        },
      };
      const tool: Tool = { name: "paged", inputSchema: { type: "object", properties: {} }, annotations: { readOnlyHint: true } };
      const entry = exposeCollection("fake", tool, paginationConfig("paged"));
      await Promise.all(
        Array.from({ length: 5 }, () => runCollection(source, interceptor, entry, { arguments: null, meta: null, startedAt: new Date() })),
      );
      assert.equal(peak, 2);
    } finally {
      store.close();
    }
  });
});

const HUGE = 9007199254740993n;

function hugePages(seen: bigint[]): PageCall {
  return (args) => {
    const offset = args.get("offset");
    assert.equal(typeof offset, "bigint");
    seen.push(offset as bigint);
    const body =
      offset === 0n
        ? `{"items": [{"i": 0}], "has_more": true, "next_offset": ${HUGE}}`
        : offset === HUGE
          ? '{"items": [{"i": 1}], "has_more": false, "next_offset": null}'
          : null;
    return Promise.resolve({
      kind: "result",
      result: body === null ? { content: [{ type: "text", text: `wrong offset ${String(offset)}` }], isError: true } : { content: [{ type: "text", text: body }] },
    });
  };
}

describe("pagination: exact offsets and long clocks", () => {
  test("offsets above 2^53 are followed exactly", async () => {
    const seen: bigint[] = [];
    const outcome = await fetchAll(hugePages(seen), paginationConfig("t"), null);
    assert.equal(outcome.reason, "complete");
    assert.equal(outcome.pages, 2);
    assert.deepEqual(seen, [0n, HUGE]);
  });

  test("an agent resume offset above 2^53 is sent exactly", async () => {
    const seen: bigint[] = [];
    const args = parseJson(`{"offset": ${HUGE}}`) as JsonObject;
    const outcome = await fetchAll(hugePages(seen), paginationConfig("t"), args);
    assert.equal(outcome.reason, "complete");
    assert.deepEqual(seen, [HUGE]);
  });

  test("a resume offset above 2^53 reaches the handle exactly", async () => {
    const config = paginationConfig("t", { max_pages: 1 });
    const seen: bigint[] = [];
    const outcome = await fetchAll(hugePages(seen), config, null);
    assert.equal(outcome.resumeOffset, HUGE);
    const limits = makeLimits();
    const store = await Store.open(limits);
    try {
      const interceptor = new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined });
      const tool: Tool = { name: "t", inputSchema: { type: "object", properties: {} }, annotations: { readOnlyHint: true } };
      const result = await runCollection(
        { fetchCollection: (entry, args) => fetchAll(hugePages([]), entry.config, args) },
        interceptor,
        exposeCollection("conformance", tool, config),
        { arguments: null, meta: null, startedAt: new Date() },
      );
      assert.ok(text(result).includes(`call again with offset=${HUGE}`), text(result));
      assert.ok(JSON.stringify(result.structuredContent).includes(`"resume_offset":${HUGE}`), JSON.stringify(result.structuredContent));
    } finally {
      store.close();
    }
  });

  test("a max_seconds beyond the timer range does not fire early", async () => {
    let pages = 0;
    const call: PageCall = async (args) => {
      await new Promise((resolve) => setTimeout(resolve, 30));
      pages += 1;
      const offset = Number(args.get("offset"));
      const more = offset < 2;
      return { kind: "result", result: { content: [{ type: "text", text: `{"items": [{"i": ${offset}}], "has_more": ${more}, "next_offset": ${more ? offset + 1 : "null"}}` }] } };
    };
    const outcome = await fetchAll(call, paginationConfig("t", { max_seconds: 3_000_000 }), null);
    assert.equal(outcome.reason, "complete");
    assert.equal(pages, 3);
  });
});
