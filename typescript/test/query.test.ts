import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { test } from "node:test";
import { promisify } from "node:util";
import { QueryRejectedError } from "../src/gate.ts";
import { Interceptor } from "../src/intercept.ts";
import { makeLimits, type Limits } from "../src/limits.ts";
import fc from "fast-check";
import { CELL_SLACK_BYTES, QueryTool, cellText, engineFailure, escapeCell, runQueryTool } from "../src/query.ts";
import { ENVELOPE_TABLE, Store } from "../src/store.ts";

const run = promisify(execFile);
const OVERSUBSCRIBED = new URL("fixtures/oversubscribed-child.ts", import.meta.url);
const UNBOUNDED = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r";

async function withStore<T>(limits: Limits, work: (store: Store) => Promise<T>): Promise<T> {
  const store = await Store.open(limits);
  try {
    return await work(store);
  } finally {
    store.close();
  }
}

async function settled(query: QueryTool): Promise<void> {
  const started = Date.now();
  while (query.inFlight > 0) {
    assert.ok(Date.now() - started < 10_000, "a timed-out query never released its slot");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

test("the store applies the database-global lockdown", async () => {
  await withStore(makeLimits({ duckdb_max_memory: "512MB" }), async (store) => {
    const reader = await store.connection.runAndReadAll(
      "SELECT current_setting('enable_external_access'), current_setting('lock_configuration'), " +
        "current_setting('autoload_known_extensions'), current_setting('autoinstall_known_extensions'), " +
        "current_setting('allow_community_extensions'), current_setting('max_memory')",
    );
    const [row] = reader.getRows();
    assert.deepEqual(row?.slice(0, 5), [false, true, false, false, false]);
    assert.match(String(row?.[5]), /^(?:488\.2 MiB|512\.0 MB|512MB)$/u);
    const other = await store.connect();
    try {
      await assert.rejects(other.run("SET enable_external_access = true"));
      await assert.rejects(other.run("SELECT * FROM read_text('/etc/hosts')"), /Permission Error/u);
    } finally {
      other.closeSync();
    }
    assert.ok(!store.allowedObjects.has(ENVELOPE_TABLE));
    const [[spill] = []] = (await store.connection.runAndReadAll("SELECT current_setting('temp_directory')")).getRows();
    assert.equal(spill, "");
  });
});

test("cells are cut before rendering without changing what is shown or reported", () => {
  fc.assert(
    fc.property(fc.string({ unit: "binary", maxLength: 80 }), fc.integer({ min: 4, max: 40 }), (value, maxBytes) => {
      assert.deepEqual(escapeCell(cellText(value, maxBytes + CELL_SLACK_BYTES), maxBytes), escapeCell(value, maxBytes));
    }),
    { numRuns: 2000 },
  );
});

test("wide cells are cut and every truncation is still reported", { timeout: 60_000 }, async () => {
  const limits = makeLimits({ max_cell_bytes: 16 });
  await withStore(limits, async (store) => {
    const text = await new QueryTool(store, limits).run(
      "WITH RECURSIVE k(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM k WHERE i < 5000) SELECT repeat_text, i FROM (SELECT 'x' || i || lpad_text AS repeat_text, i FROM k, (SELECT 'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy' AS lpad_text))",
      2,
    );
    assert.match(text, /2 row\(s\) shown\. Additional rows exist beyond max_rows; the exact number is not known\. 2 cell\(s\) truncated/u);
  });
});

test("a runaway query is interrupted at the deadline and its connection released", { timeout: 30_000 }, async () => {
  const limits = makeLimits({ query_timeout_seconds: 0.3 });
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    const started = Date.now();
    await assert.rejects(query.run(UNBOUNDED), (error: unknown) => {
      assert.ok(error instanceof QueryRejectedError);
      assert.match(error.message, /timeout/u);
      return true;
    });
    assert.ok(Date.now() - started < 3_000, `took ${Date.now() - started} ms`);
    await settled(query);
    assert.match(await query.run("SELECT 41 + 1 AS answer"), /\| 42 \|/u);
  });
});

test("deadlines hold when more queries arrive than the pool can run", { timeout: 60_000 }, async () => {
  const limits = makeLimits({ query_timeout_seconds: 0.5 });
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    assert.ok(query.capacity >= 1);
    const started = Date.now();
    const outcomes = await Promise.allSettled(Array.from({ length: 8 }, () => query.run(UNBOUNDED)));
    const elapsed = Date.now() - started;
    for (const outcome of outcomes) {
      assert.equal(outcome.status, "rejected");
      assert.match(String((outcome as PromiseRejectedResult).reason), /timeout/u);
    }
    assert.ok(elapsed < 5_000, `took ${elapsed} ms`);
    await settled(query);
    assert.match(await query.run("SELECT 1 AS one"), /1 row\(s\) shown\./u);
  });
});

test("a query never runs past its deadline while a write is in flight", { timeout: 30_000 }, async () => {
  const limits = makeLimits({ query_timeout_seconds: 0.3 });
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    const interceptor = new Interceptor(store, limits, { queryAvailable: true });
    const slow = query.run(UNBOUNDED).catch((error: unknown) => error);
    const handle = await interceptor.intercept({
      server: "s",
      tool: "t",
      mounted: "s__t__000000000000",
      arguments: null,
      result: { content: [{ type: "text", text: '[{"a": 1}, {"a": 2}]' }] },
      meta: { conversationId: "write-during-query" },
      startedAt: new Date(),
    });
    assert.match(String(await slow), /timeout/u);
    const structured = handle.structuredContent as { tables: { name: string }[] };
    const [table] = structured.tables;
    assert.ok(table !== undefined);
    assert.match(await query.run(`SELECT sum(a) AS total FROM "${table.name}"`), /\| 3 \|/u);
  });
});

test("empty, blank and comment-only SQL is refused as no statement", async () => {
  const limits = makeLimits();
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    for (const sql of ["", "   ", "-- nothing", "/* nothing */", " ;", ";;"]) {
      await assert.rejects(query.run(sql), /no statement found/u, JSON.stringify(sql));
    }
  });
});

test("an unknown table is refused before binding, the same way whether or not it exists", async () => {
  const limits = makeLimits();
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    const missing = await runQueryTool(query, { sql: "SELECT * FROM no_such_table" });
    const physical = await runQueryTool(query, { sql: `SELECT * FROM ${ENVELOPE_TABLE}` });
    const strip = (text: string | undefined): string => String(text).replace(/'[^']*'/u, "'x'");
    assert.equal(missing.isError, true);
    assert.equal(physical.isError, true);
    assert.equal(strip(missing.content[0]?.text), strip(physical.content[0]?.text));
  });
});

test("DECIMAL and big integers render exactly", async () => {
  const limits = makeLimits();
  await withStore(limits, async (store) => {
    const query = new QueryTool(store, limits);
    const text = await query.run(
      "SELECT 12345678901234567.12::DECIMAL(38,2) AS d, 170141183460469231731687303715884105727::HUGEINT AS h, NULL AS n, '' AS e",
    );
    assert.match(text, /\| 12345678901234567\.12 \| 170141183460469231731687303715884105727 \| NULL \| '' \|/u);
  });
});

test("an over-subscribed pool still answers at the deadline and frees every slot", { timeout: 60_000 }, async () => {
  const { stdout } = await run(process.execPath, [OVERSUBSCRIBED.pathname, "12"], {
    env: { ...process.env, UV_THREADPOOL_SIZE: "4" },
    timeout: 30_000,
  });
  const report = JSON.parse(stdout) as { timeouts: number; responded: number; released: number; answered: boolean };
  assert.equal(report.timeouts, 12);
  assert.ok(report.responded < 3_000, `responded after ${report.responded} ms`);
  assert.ok(report.released < 10_000, `released after ${report.released} ms`);
  assert.equal(report.answered, true);
});

function tableOf(result: { structuredContent?: unknown }): string {
  const [table] = (result.structuredContent as { tables: { name: string }[] }).tables;
  assert.ok(table !== undefined, "expected a table");
  return table.name;
}

test("a query queued behind a slow one cannot learn another scope's table from the engine", { timeout: 30_000 }, async () => {
  const limits = makeLimits({ max_session_calls: 3, query_timeout_seconds: 1 });
  await withStore(limits, async (store) => {
    const interceptor = new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined });
    const query = new QueryTool(store, limits, { concurrency: 1 });
    const call = (conversationId: string, id: number) =>
      interceptor.intercept({
        server: "srv",
        tool: "rows",
        mounted: "srv__rows__000000000000",
        arguments: null,
        result: { content: [{ type: "text", text: `[{"id": ${id}}]` }] },
        meta: { conversationId },
        startedAt: new Date(),
      });
    const mine = tableOf(await call("scope-a", 1));
    const theirs = [tableOf(await call("scope-b", 2))];
    const blocker = runQueryTool(query, { sql: UNBOUNDED });
    await new Promise((resolve) => setTimeout(resolve, 400));
    const queued = runQueryTool(query, { sql: `SELECT * FROM "${mine}"` });
    await new Promise((resolve) => setTimeout(resolve, 50));
    theirs.push(tableOf(await call("scope-b", 3)), tableOf(await call("scope-b", 4)));
    assert.match(String((await blocker).content[0]?.text), /timeout/u);
    const result = await queued;
    const text = String(result.content[0]?.text);
    assert.equal(result.isError, true, text);
    assert.doesNotMatch(text, /Did you mean/u);
    for (const name of theirs) {
      assert.ok(!text.includes(name), `the error named ${name}: ${text}`);
    }
    assert.ok(!text.includes("scope"), text);
  });
});

test("engine errors never carry suggestions, and a vanished table is reported as no longer available", () => {
  const missing = engineFailure(new Error('Catalog Error: Table with name x does not exist!\nDid you mean "other__0002"?\n\nLINE 1: SELECT'));
  assert.equal(missing.message, "a table this query names is no longer available; it may have been evicted");
  const binder = engineFailure(new Error('Binder Error: Referenced column "lenght" not found\nDid you mean "length"?\nLINE 1'));
  assert.doesNotMatch(binder.message, /Did you mean/u);
  assert.match(binder.message, /^BinderError: Binder Error: Referenced column/u);
});

test("a misspelt function is reported without a suggestion", async () => {
  const limits = makeLimits();
  await withStore(limits, async (store) => {
    const result = await runQueryTool(new QueryTool(store, limits), { sql: "SELECT lenght('x')" });
    assert.equal(result.isError, true);
    assert.doesNotMatch(String(result.content[0]?.text), /Did you mean/u);
  });
});
