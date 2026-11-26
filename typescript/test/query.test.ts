import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { test } from "node:test";
import { promisify } from "node:util";
import { QueryRejectedError } from "../src/gate.ts";
import { Interceptor } from "../src/intercept.ts";
import { makeLimits, type Limits } from "../src/limits.ts";
import { QueryTool, runQueryTool } from "../src/query.ts";
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
