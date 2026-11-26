import assert from "node:assert/strict";
import { test } from "node:test";
import { Interceptor, type InterceptRequest } from "../src/intercept.ts";
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
const deepObject = `{"deep": ${nested}}`;

function assertHandle(result: ToolResult, original: ToolResult): { tables: { name: string }[]; flat_reason: string | null } {
  assert.notEqual(result, original);
  assert.notEqual(result.isError, true);
  const structured = result.structuredContent as { tables: { name: string }[]; flat_reason: string | null } | undefined;
  assert.ok(structured !== undefined, "expected a handle");
  return structured;
}

test("a deeply nested structuredContent is materialized without failing the call", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const original: ToolResult = { content: [{ type: "text", text: "prose" }], structuredContent: { opaque: true } };
    const handle = assertHandle(await interceptor.intercept(request(original, { structured: { json: deepObject } })), original);
    assert.deepEqual(events, []);
    assert.equal(handle.flat_reason, null);
    const [table] = handle.tables;
    assert.ok(table !== undefined);
    const query = new QueryTool(store, makeLimits());
    assert.match(await query.run(`SELECT count(*), max(length(deep::VARCHAR)) FROM "${table.name}"`), new RegExp(`\\| 1 \\| ${DEEP * 2} \\|`, "u"));
  });
});

test("a deeply nested text channel is materialized without failing the call", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const original: ToolResult = { content: [{ type: "text", text: deepObject }] };
    const handle = assertHandle(await interceptor.intercept(request(original)), original);
    assert.deepEqual(events, []);
    assert.equal(handle.tables.length, 1);
    assert.deepEqual(await flatReasons(store), ["null"]);
  });
});

test("a selection failure passes the original result through and is recorded", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const hostile = {
      type: "text",
      get text(): string {
        throw new Error("synthetic selection failure");
      },
    };
    const original: ToolResult = { content: [hostile] };
    assert.equal(await interceptor.intercept(request(original)), original);
    assert.deepEqual(events, ["selection_failed"]);
    const [reason] = await flatReasons(store);
    assert.match(String(reason), /^selection_failed: Error: synthetic selection failure/u);
    const next = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }));
    assert.equal(tables(next).length, 1);
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
    assert.deepEqual(events, ["commit_failed", "envelope_payload_failed", "envelope_write_failed"]);
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

test("an envelope that cannot hold its payload is still written as metadata", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const commit = store.commitCall.bind(store);
    store.commitCall = async (record, plans) => {
      if (record.payload.value !== undefined) {
        throw new Error("synthetic payload failure");
      }
      return commit(record, plans);
    };
    const result = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }));
    const structured = result.structuredContent as { tables: unknown[]; flat_reason: string };
    assert.deepEqual(structured.tables, []);
    assert.match(structured.flat_reason, /^load_failed: .*; payload_dropped: Error: synthetic payload failure/u);
    assert.deepEqual(events, ["commit_failed", "envelope_payload_failed"]);
    const reader = await store.connection.runAndReadAll(`SELECT result IS NULL, result_text IS NULL, args IS NULL FROM ${ENVELOPE_TABLE}`);
    assert.deepEqual(reader.getRows(), [[true, true, true]]);
  });
});

for (const [label, text] of [
  ["a lone surrogate key", '[{"\\ud800": 1}]'],
  ["a lone surrogate value", '[{"a": "\\udc00"}]'],
  ["a raw lone surrogate in prose", '[{"a": 1}] \ud800 trailing'],
] as const) {
  test(`${label} is recorded and materialized`, async () => {
    await withInterceptor(makeLimits(), async (interceptor, store, events) => {
      const original: ToolResult = { content: [{ type: "text", text }] };
      const result = await interceptor.intercept(request(original));
      assert.notEqual(result, original);
      assert.deepEqual(events, []);
      const reader = await store.connection.runAndReadAll(`SELECT count(*) FROM ${ENVELOPE_TABLE}`);
      assert.deepEqual(reader.getRows(), [[1n]]);
    });
  });
}

test("deeply nested tool arguments never fail the call", async () => {
  await withInterceptor(makeLimits({ max_session_bytes: 100_000_000 }), async (interceptor, store, events) => {
    let args: unknown = 1;
    for (let depth = 0; depth < DEEP; depth += 1) {
      args = { next: args };
    }
    const result = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }, { arguments: args }));
    assert.equal(tables(result).length, 1);
    assert.deepEqual(events, []);
    const reader = await store.connection.runAndReadAll(`SELECT length(args::VARCHAR) > ${DEEP} FROM ${ENVELOPE_TABLE}`);
    assert.deepEqual(reader.getRows(), [[true]]);
  });
});

test("cyclic tool arguments are left unrecorded rather than failing the call", async () => {
  await withInterceptor(makeLimits(), async (interceptor, _store, events) => {
    const args: Record<string, unknown> = {};
    args["self"] = args;
    const result = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }, { arguments: args }));
    assert.equal(tables(result).length, 1);
    assert.deepEqual(events, ["arguments_unrecorded"]);
  });
});

test("malformed results and metadata never escape interception", async () => {
  await withInterceptor(makeLimits(), async (interceptor, store, events) => {
    const hostileMeta = {
      get conversationId(): string {
        throw new Error("synthetic meta failure");
      },
    };
    const shapes: ToolResult[] = [
      { content: null as unknown as ToolResult["content"] },
      { content: [null, { type: "text", text: "[]" }] as unknown as ToolResult["content"] },
      { content: [{ type: 7, text: "[]" }] as unknown as ToolResult["content"] },
    ];
    for (const original of shapes) {
      assert.equal(await interceptor.intercept(request(original)), original);
    }
    const handled = await interceptor.intercept(request({ content: [{ type: "text", text: '[{"id": 1}]' }] }, { meta: hostileMeta }));
    assert.equal(tables(handled).length, 1);
    assert.deepEqual(events, ["scope_unreadable"]);
    const reader = await store.connection.runAndReadAll(`SELECT content_kinds FROM ${ENVELOPE_TABLE} ORDER BY seq`);
    assert.deepEqual(reader.getRows().map(([kinds]) => String(kinds)), ["[]", "['unknown', 'text']", "['unknown']", "['text']"]);
  });
});
