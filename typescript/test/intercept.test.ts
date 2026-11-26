import assert from "node:assert/strict";
import { test } from "node:test";
import { Interceptor, type InterceptRequest } from "../src/intercept.ts";
import { parseJson } from "../src/json.ts";
import { makeLimits, type Limits } from "../src/limits.ts";
import type { ToolResult } from "../src/models.ts";
import { QueryTool } from "../src/query.ts";
import { ENVELOPE_TABLE, Store } from "../src/store.ts";

const DEEP = 200_000;

function request(result: ToolResult, extra: Partial<InterceptRequest> = {}): InterceptRequest {
  return {
    server: "fake",
    tool: "rows",
    mounted: "fake__rows__000000000000",
    arguments: null,
    result,
    meta: null,
    startedAt: new Date(),
    ...extra,
  };
}

async function withInterceptor<T>(
  limits: Limits,
  work: (interceptor: Interceptor, store: Store, events: string[]) => Promise<T>,
): Promise<T> {
  const store = await Store.open(limits);
  const events: string[] = [];
  try {
    return await work(new Interceptor(store, limits, { queryAvailable: true, logger: (event) => events.push(event) }), store, events);
  } finally {
    store.close();
  }
}

async function flatReasons(store: Store): Promise<string[]> {
  const reader = await store.connection.runAndReadAll(`SELECT flat_reason FROM ${ENVELOPE_TABLE} ORDER BY started_at, seq`);
  return reader.getRows().map(([reason]) => String(reason));
}

function tables(result: ToolResult): { name: string }[] {
  return (result.structuredContent as { tables: { name: string }[] }).tables;
}

const nested = `${"[".repeat(DEEP)}${"]".repeat(DEEP)}`;

test("the lossless parser really overflows the stack on this depth", () => {
  assert.throws(() => parseJson(nested), RangeError);
});

test("a structuredContent too deep to parse passes through and is recorded", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const original: ToolResult = { content: [{ type: "text", text: "prose" }], structuredContent: { opaque: true } };
    const result = await interceptor.intercept(request(original, { structured: { json: `{"deep": ${nested}}` } }));
    assert.equal(result, original);
    assert.deepEqual(events, ["selection_failed"]);
    const [reason] = await flatReasons(store);
    assert.match(String(reason), /^selection_failed: RangeError/u);
    const next = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }));
    assert.equal(tables(next).length, 1);
  });
});

test("a text channel too deep to parse passes through and is recorded", async () => {
  await withInterceptor(makeLimits({ max_payload_bytes: 1_000_000, max_session_bytes: 10_000_000 }), async (interceptor, store) => {
    const original: ToolResult = { content: [{ type: "text", text: nested }] };
    const result = await interceptor.intercept(request(original));
    assert.equal(result, original);
    assert.match(String((await flatReasons(store))[0]), /^selection_failed: RangeError/u);
  });
});

test("a failed commit degrades to an envelope-only handle and does not wedge later calls", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const commit = store.commitCall.bind(store);
    let failures = 1;
    store.commitCall = async (record, plans) => {
      if (failures > 0 && plans.length > 0) {
        failures -= 1;
        throw new Error("synthetic insert failure");
      }
      return commit(record, plans);
    };
    const first = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }));
    assert.notEqual(first.isError, true);
    const structured = first.structuredContent as { tables: unknown[]; flat_reason: string };
    assert.deepEqual(structured.tables, []);
    assert.match(structured.flat_reason, /^load_failed: Error: synthetic insert failure/u);
    assert.deepEqual(events, ["commit_failed"]);
    const second = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 2}]' }] }));
    const [table] = tables(second);
    assert.ok(table !== undefined);
    const query = new QueryTool(store, makeLimits());
    assert.match(await query.run(`SELECT id FROM "${table.name}"`), /\| 2 \|/u);
  });
});

test("a failed envelope write returns the downstream result unmodified", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    store.commitCall = () => Promise.reject(new Error("synthetic envelope failure"));
    const original: ToolResult = { content: [{ type: "text", text: '[{"id": 1}]' }] };
    assert.equal(await interceptor.intercept(request(original)), original);
    assert.deepEqual(events, ["commit_failed", "envelope_write_failed"]);
  });
});

test("structuredContent text keeps big integers and the int-float distinction", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store) => {
    const json = '{"items": [{"big": 9007199254740993, "f": 1.0}, {"big": 2, "f": 2.5}]}';
    const result = await interceptor.intercept(
      request({ content: [{ type: "text", text: "two rows" }], structuredContent: JSON.parse(json) as Record<string, unknown> }, {
        structured: { json },
      }),
    );
    const structured = result.structuredContent as {
      source_channel: string;
      tables: { name: string; columns: { name: string; type: string; exact: boolean }[] }[];
    };
    assert.equal(structured.source_channel, "structured");
    const [table] = structured.tables;
    assert.ok(table !== undefined);
    assert.deepEqual(
      table.columns.map(({ name, type, exact }) => [name, type, exact]),
      [
        ["big", "BIGINT", false],
        ["f", "DOUBLE", false],
      ],
    );
    const query = new QueryTool(store, makeLimits());
    assert.match(await query.run(`SELECT max(big) FROM "${table.name}"`), /\| 9007199254740993 \|/u);
  });
});

test("plain structuredContent is still accepted when no lossless form is supplied", async () => {
  await withInterceptor(makeLimits(), async (interceptor) => {
    const result = await interceptor.intercept(
      request({ content: [], structuredContent: { items: [{ a: 1 }, { a: 2.5 }] } }),
    );
    const [table] = (result.structuredContent as { tables: { columns: { type: string }[] }[] }).tables;
    assert.equal(table?.columns[0]?.type, "DOUBLE");
  });
});

test("admission bounds the whole pipeline, not only the write", async () => {
  const limits = makeLimits({ max_concurrent_materializations: 1 });
  await withInterceptor(limits, async (interceptor, store) => {
    const commit = store.commitCall.bind(store);
    const ticket = store.nextRetentionSeq.bind(store);
    let admitted = 0;
    store.nextRetentionSeq = () => {
      admitted += 1;
      return ticket();
    };
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    let commitStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      commitStarted = resolve;
    });
    store.commitCall = async (record, plans) => {
      commitStarted();
      await blocked;
      return commit(record, plans);
    };
    const first = interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }));
    await started;
    const second = interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 2}]' }] }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(admitted, 1);
    release();
    await Promise.all([first, second]);
    assert.equal(admitted, 2);
  });
});
