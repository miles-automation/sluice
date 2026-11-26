import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { test } from "node:test";
import { promisify } from "node:util";
import {
  LIBUV_DEFAULT_THREADPOOL_SIZE,
  THREADPOOL_ENV,
  configureThreadpool,
  queryConcurrency,
  threadpoolSize,
} from "../src/threadpool.ts";

const run = promisify(execFile);
const CHILD = new URL("fixtures/threadpool-child.ts", import.meta.url);

test("threadpool size reads the environment and falls back to the libuv default", () => {
  assert.equal(threadpoolSize({}), LIBUV_DEFAULT_THREADPOOL_SIZE);
  assert.equal(threadpoolSize({ [THREADPOOL_ENV]: "8" }), 8);
  assert.equal(threadpoolSize({ [THREADPOOL_ENV]: "zero" }), LIBUV_DEFAULT_THREADPOOL_SIZE);
  assert.equal(threadpoolSize({ [THREADPOOL_ENV]: "0" }), LIBUV_DEFAULT_THREADPOOL_SIZE);
  assert.equal(threadpoolSize({ [THREADPOOL_ENV]: "5000" }), 1024);
});

test("query concurrency stays below the pool size", () => {
  assert.equal(queryConcurrency(4), 2);
  assert.equal(queryConcurrency(8), 6);
  assert.equal(queryConcurrency(2), 1);
  assert.equal(queryConcurrency(1), 1);
});

test("configureThreadpool never overrides an operator's setting", () => {
  const environment: Record<string, string | undefined> = { [THREADPOOL_ENV]: "3" };
  assert.equal(configureThreadpool(8, environment), 3);
  const empty: Record<string, string | undefined> = {};
  assert.equal(configureThreadpool(6, empty), 6);
  assert.equal(empty[THREADPOOL_ENV], "6");
});

test("setting the pool size before the first DuckDB call takes effect in that process", { timeout: 60_000 }, async () => {
  const environment = { ...process.env };
  delete environment[THREADPOOL_ENV];
  const { stdout } = await run(process.execPath, [CHILD.pathname, "2"], { env: environment });
  assert.deepEqual(JSON.parse(stdout), { size: 2, queued: true });
});

test("control: with a larger pool the same two slow queries do not delay a third", { timeout: 60_000 }, async () => {
  const { stdout } = await run(process.execPath, [CHILD.pathname, "2"], { env: { ...process.env, [THREADPOOL_ENV]: "8" } });
  assert.deepEqual(JSON.parse(stdout), { size: 8, queued: false });
});
