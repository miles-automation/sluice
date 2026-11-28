import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { test } from "node:test";
import { serialize } from "node:v8";
import { fileURLToPath } from "node:url";
import { Interceptor } from "../src/intercept.ts";
import { makeLimits, type Limits } from "../src/limits.ts";
import type { ToolResult } from "../src/models.ts";
import { mountedName } from "../src/naming.ts";
import { QueryRejectedError } from "../src/gate.ts";
import { QueryTool } from "../src/query.ts";
import { FrameReader } from "../src/ipc.ts";
import { replyFrame } from "../src/store-server.ts";
import { Store } from "../src/store.ts";
import {
  StoreProcess,
  SupervisedStore,
  dataLimit,
  describeBytes,
  memorySampler,
  openStore,
  parseFootprint,
  parseStatusRss,
} from "../src/supervisor.ts";

const CAP = 256 * 1024 * 1024;
const LIMITS = makeLimits({ store_max_memory: CAP, duckdb_max_memory: "64MB" });
const MOUNTED = mountedName("srv", "rows");
const SUPERVISOR = fileURLToPath(new URL("../src/supervisor.ts", import.meta.url));
const LIMITS_MODULE = fileURLToPath(new URL("../src/limits.ts", import.meta.url));
const QUERY_MODULE = fileURLToPath(new URL("../src/query.ts", import.meta.url));

interface Structured {
  readonly call_id: string;
  readonly scope_id: string;
  readonly envelope_table: string;
  readonly flat_reason: string | null;
  readonly tables: readonly { readonly name: string }[];
}

function hostile(table: string): string {
  let inner = "s";
  for (let level = 0; level < 13; level += 1) {
    inner = `replace(${inner}, 'a', 'aaaa')`;
  }
  return `SELECT sum(length(${inner})) FROM "${table}"`;
}

function seedRows(): unknown[] {
  return Array.from({ length: 20 }, (_, index) => ({ id: index + 1, s: "a" }));
}

async function materialize(interceptor: Interceptor, rows: unknown[], meta: unknown = null): Promise<Structured> {
  const original: ToolResult = { content: [{ type: "text", text: JSON.stringify({ items: rows }) }] };
  const result = await interceptor.intercept({
    server: "srv",
    tool: "rows",
    mounted: MOUNTED,
    arguments: null,
    result: original,
    meta,
    startedAt: new Date(),
  });
  assert.notEqual(result.isError, true);
  assert.ok(result.structuredContent !== undefined, "expected a handle");
  return result.structuredContent as unknown as Structured;
}

const WATCHER_KILL_BYTES = 2 * 1024 ** 3;

function watch(store: SupervisedStore): () => Promise<void> {
  const sampler = memorySampler();
  let running = true;
  const loop = (async () => {
    while (running) {
      const pid = store.process?.pid;
      if (pid !== undefined && pid > 0) {
        const used = await sampler.sample(pid);
        if (used !== null && used > WATCHER_KILL_BYTES) {
          process.kill(pid, "SIGKILL");
          assert.fail(`the outside watcher killed the store process at ${describeBytes(used)}`);
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  })();
  return async () => {
    running = false;
    await loop;
  };
}

async function withStore(
  limits: Limits,
  work: (store: SupervisedStore, interceptor: Interceptor, query: QueryTool) => Promise<void>,
): Promise<void> {
  const store = await SupervisedStore.open(limits);
  const stop = watch(store);
  try {
    await work(store, new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined }), new QueryTool(store, limits));
    await new Promise((resolve) => setTimeout(resolve, 300));
  } finally {
    await stop();
    await store.close();
  }
}

async function rejection(work: Promise<unknown>): Promise<string> {
  try {
    await work;
  } catch (error) {
    assert.ok(error instanceof QueryRejectedError, String(error));
    return error.message;
  }
  assert.fail("expected the query to be rejected");
}

test("a hostile query resets the store and the server keeps serving", async () => {
  await withStore(LIMITS, async (store, interceptor, query) => {
    const handle = await materialize(interceptor, seedRows());
    const table = handle.tables[0]?.name ?? "";
    const process = store.process;
    assert.ok(process !== null);
    const message = await rejection(query.run(hostile(table)));
    assert.match(message, /^store reset:/u);
    assert.ok(message.includes("store_max_memory") && message.includes(String(CAP)), message);
    assert.equal(store.resets, 1);
    assert.match(await query.run("SELECT 42 AS answer"), /1 row\(s\) shown\./u);
    const fresh = await materialize(interceptor, [{ id: 7 }]);
    assert.match(await query.run(`SELECT id FROM "${fresh.tables[0]?.name ?? ""}"`), /\| 7 \|/u);
  });
});

test("every pre-reset handle fails loudly", async () => {
  const meta = { conversationId: "reset-conversation" };
  await withStore(LIMITS, async (_store, interceptor, query) => {
    const before = await materialize(interceptor, seedRows(), meta);
    const table = before.tables[0]?.name ?? "";
    assert.match(await rejection(query.run(hostile(table))), /store reset/u);
    const after = await materialize(interceptor, [{ id: 1 }], meta);
    assert.equal(after.scope_id, before.scope_id);
    assert.notEqual(after.envelope_table, before.envelope_table);
    assert.notEqual(after.tables[0]?.name, table);
    for (const sql of [
      `SELECT count(*) FROM "${table}"`,
      `SELECT count(*) FROM "${before.envelope_table}"`,
      `SELECT count(*) FROM ${before.envelope_table.toUpperCase()}`,
    ]) {
      assert.match(await rejection(query.run(sql)), /lost when the store was reset/u);
    }
    assert.match(await query.run(`SELECT count(*) FROM "${after.envelope_table}"`), /\| 1 \|/u);
  });
});

test("a materialization racing a reset keeps the tool call working", async (context) => {
  const original = StoreProcess.prototype.request;
  let killed = false;
  context.after(() => {
    StoreProcess.prototype.request = original;
  });
  StoreProcess.prototype.request = function (this: StoreProcess, message) {
    if (message["op"] === "commit" && !killed) {
      killed = true;
      process.kill(this.pid, "SIGKILL");
    }
    return original.call(this, message);
  };
  await withStore(LIMITS, async (store, interceptor, query) => {
    const handle = await materialize(interceptor, seedRows());
    assert.ok(killed);
    assert.equal(store.resets, 1);
    assert.deepEqual(handle.tables, []);
    assert.match(String(handle.flat_reason), /^load_failed: StoreResetError: store reset/u);
    const text = await query.run(`SELECT count(*) FROM "${handle.envelope_table}" WHERE call_id = '${handle.call_id}'`);
    assert.match(text, /\| 1 \|/u);
  });
});

test("materializations concurrent with a hostile query never fail", async () => {
  await withStore(LIMITS, async (_store, interceptor, query) => {
    const seed = await materialize(interceptor, seedRows());
    const attack = rejection(query.run(hostile(seed.tables[0]?.name ?? "")));
    const handles: Structured[] = [];
    for (let index = 0; index < 20; index += 1) {
      const rows = Array.from({ length: 200 }, () => ({ id: index, pad: "x".repeat(2000) }));
      handles.push(await materialize(interceptor, rows));
    }
    assert.match(await attack, /^store reset:/u);
    assert.equal(handles.length, 20);
    for (const handle of handles) {
      assert.ok(handle.tables.length > 0 || String(handle.flat_reason).startsWith("load_failed"), String(handle.flat_reason));
    }
  });
});

test("a query timeout interrupts inside the store without a reset", async () => {
  const limits = makeLimits({ store_max_memory: CAP, duckdb_max_memory: "64MB", query_timeout_seconds: 0.5 });
  await withStore(limits, async (store, interceptor, query) => {
    const handle = await materialize(
      interceptor,
      Array.from({ length: 1000 }, (_, a) => ({ a })),
    );
    const table = handle.tables[0]?.name ?? "";
    const started = performance.now();
    const message = await rejection(
      query.run(`SELECT count(*) FROM "${table}" x, "${table}" y, "${table}" z WHERE x.a + y.a + z.a > 0`),
    );
    assert.match(message, /timeout/u);
    assert.ok(performance.now() - started < 3000);
    assert.equal(store.resets, 0);
    assert.match(await query.run(`SELECT count(*) FROM "${table}"`), /\| 1000 \|/u);
  });
});

test("a timed-out hostile query stays watched until DuckDB stops", async () => {
  const limits = makeLimits({ store_max_memory: CAP, duckdb_max_memory: "64MB", query_timeout_seconds: 0.05 });
  await withStore(limits, async (store, interceptor, query) => {
    const handle = await materialize(interceptor, seedRows());
    const message = await rejection(query.run(hostile(handle.tables[0]?.name ?? "")));
    assert.match(message, /^store reset:/u);
    assert.equal(store.resets, 1);
    assert.equal(query.inFlight, 0);
  });
});

test("gate rejections and engine errors cross the process boundary", async () => {
  await withStore(LIMITS, async (store, _interceptor, query) => {
    assert.match(await rejection(query.run("SELECT * FROM sluice_calls")), /unknown table/u);
    assert.match(await rejection(query.run("CREATE TABLE x (a INT)")), /only SELECT|could not be analyzed/u);
    const engine = await rejection(query.run("SELECT lenght('x')"));
    assert.match(engine, /CatalogError/u);
    assert.ok(!engine.includes("Did you mean"));
    assert.equal(store.resets, 0);
  });
});

function script(body: string): string {
  return [
    `import { SupervisedStore } from ${JSON.stringify(SUPERVISOR)};`,
    `import { makeLimits } from ${JSON.stringify(LIMITS_MODULE)};`,
    `import { QueryTool } from ${JSON.stringify(QUERY_MODULE)};`,
    `const limits = makeLimits({ store_max_memory: ${CAP}, duckdb_max_memory: "64MB" });`,
    body,
  ].join("\n");
}

test("the store process never writes to stdout", () => {
  const directory = mkdtempSync(join(tmpdir(), "sluice-stdout-"));
  try {
    const preload = join(directory, "marker.mjs");
    writeFileSync(
      preload,
      'if (process.argv[1]?.includes("store-worker")) { process.stdout.write("CHILD-STDOUT-MARKER\\n"); console.log("CHILD-CONSOLE-MARKER"); }\n',
    );
    const completed = spawnSync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        script(
          [
            "const store = await SupervisedStore.open(limits);",
            'await new QueryTool(store, limits).run("SELECT 1");',
            'store.process?.kill("test");',
            'await new QueryTool(store, limits).run("SELECT 1").catch(() => undefined);',
            "await store.close();",
          ].join("\n"),
        ),
      ],
      { encoding: "utf8", env: { ...process.env, NODE_OPTIONS: `--import=${preload}` }, timeout: 60_000 },
    );
    assert.equal(completed.status, 0, completed.stderr);
    assert.equal(completed.stdout, "");
    assert.ok(completed.stderr.includes("CHILD-STDOUT-MARKER"), completed.stderr);
    assert.ok(completed.stderr.includes("CHILD-CONSOLE-MARKER"), completed.stderr);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("the store process exits when its parent dies", async () => {
  const parent = spawn(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      script(
        [
          "const store = await SupervisedStore.open(limits);",
          "process.stdout.write(`${store.process?.pid}\\n`);",
          "setInterval(() => undefined, 1000);",
        ].join("\n"),
      ),
    ],
    { stdio: ["ignore", "pipe", "inherit"] },
  );
  let line = "";
  for await (const text of createInterface({ input: parent.stdout })) {
    line = text;
    break;
  }
  const child = Number(line);
  assert.ok(child > 0);
  parent.kill("SIGKILL");
  const deadline = performance.now() + 10_000;
  while (performance.now() < deadline) {
    try {
      process.kill(child, 0);
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  process.kill(child, "SIGKILL");
  assert.fail("the store process outlived its parent");
});

test("the store process runs under RLIMIT_DATA on Linux", { skip: process.platform !== "linux" }, async () => {
  await withStore(LIMITS, async (store) => {
    const pid = store.process?.pid ?? -1;
    const limits = readFileSync(`/proc/${pid}/limits`, "ascii");
    const line = limits.split("\n").find((row) => row.startsWith("Max data size")) ?? "";
    assert.ok(line.includes(String(dataLimit(CAP))), line);
  });
});

test("the memory sampler reads this platform", async () => {
  const used = await memorySampler().sample(process.pid);
  assert.ok(used !== null && used > 1024 * 1024, describeBytes(used ?? 0));
});

test("query replies stay within the output budget", async (context) => {
  const original = StoreProcess.prototype.start;
  const sizes: number[] = [];
  context.after(() => {
    StoreProcess.prototype.start = original;
  });
  StoreProcess.prototype.start = function (this: StoreProcess, message) {
    const request = original.call(this, message);
    return { id: request.id, reply: request.reply.then((reply) => (sizes.push(serialize(reply).length), reply)) };
  };
  const limits = makeLimits({ store_max_memory: 2 * CAP, duckdb_max_memory: "64MB" });
  await withStore(limits, async (_store, interceptor, query) => {
    const handle = await materialize(
      interceptor,
      Array.from({ length: 150 }, (_, id) => ({ id, s: "\u{1F600}".repeat(200) })),
    );
    const table = handle.tables[0]?.name ?? "";
    const columns = Array.from({ length: 300 }, (_, index) => `s AS "c${index}"`).join(", ");
    const text = await query.run(`SELECT ${columns} FROM "${table}"`);
    assert.ok(Math.max(...sizes) < 4 * limits.query_max_bytes, String(sizes));
    assert.ok(Buffer.byteLength(text) <= limits.query_max_bytes);
    const notices = text.split("\n").at(-1) ?? "";
    const shown = Number(/^([0-9]+) row\(s\) shown\./u.exec(notices)?.[1]);
    const omitted = Number(/([0-9]+) row\(s\) omitted/u.exec(notices)?.[1]);
    assert.equal(shown + omitted, limits.query_max_rows, notices);
    assert.match(notices, /Additional rows exist beyond max_rows/u);
    const narrow = await query.run(`SELECT "id" FROM "${table}" ORDER BY "id"`, 3);
    assert.ok(narrow.endsWith("3 row(s) shown. Additional rows exist beyond max_rows; the exact number is not known."), narrow);
  });
});

test("an oversized reply becomes a query error", () => {
  const encoded = replyFrame({ id: 7, ok: true, rows: [["x".repeat(100)]] }, 64);
  const reply = new FrameReader().push(encoded)[0] ?? {};
  assert.equal(reply["id"], 7);
  assert.equal(reply["ok"], false);
  assert.equal(reply["kind"], "rejected");
  assert.match(String(reply["message"]), /over the 64 byte limit/u);
});

test("a reset racing the stale check still reports the reset", async () => {
  await withStore(LIMITS, async (store, interceptor, query) => {
    const handle = await materialize(interceptor, seedRows());
    const table = handle.tables[0]?.name ?? "";
    const pending = query.run(`SELECT count(*) FROM "${table}"`);
    store.process?.kill("test reset");
    assert.match(await rejection(pending), /lost when the store was reset/u);
    assert.match(await rejection(query.run(`SELECT count(*) FROM "${table}"`)), /lost when the store was reset/u);
  });
});

test("a table evicted before a reset is still reported as evicted", async () => {
  const limits = makeLimits({ store_max_memory: CAP, duckdb_max_memory: "64MB", max_payload_bytes: 3000, max_session_bytes: 3500 });
  await withStore(limits, async (store, interceptor, query) => {
    const first = await materialize(interceptor, [{ id: 1, padding: "x".repeat(400) }]);
    await materialize(interceptor, [{ id: 2, padding: "x".repeat(400) }]);
    const table = first.tables[0]?.name ?? "";
    assert.match(await rejection(query.run(`SELECT * FROM "${table}"`)), /was evicted/u);
    store.process?.kill("test reset");
    assert.match(await query.run("SELECT 1"), /1 row\(s\) shown\./u);
    assert.equal(store.resets, 1);
    assert.match(await rejection(query.run(`SELECT * FROM "${table}"`)), /was evicted/u);
    assert.match(await rejection(query.run(`SELECT * FROM "${first.envelope_table}"`)), /lost when the store was reset/u);
  });
});

for (const platform of ["win32", "freebsd", "aix"]) {
  test(`${platform} falls back to the in-process store loudly`, async () => {
    const lines: string[] = [];
    const write = process.stderr.write.bind(process.stderr);
    process.stderr.write = ((chunk: string | Uint8Array) => (lines.push(String(chunk)), true)) as typeof process.stderr.write;
    let store: Awaited<ReturnType<typeof openStore>>;
    try {
      store = await openStore(LIMITS, platform);
    } finally {
      process.stderr.write = write;
    }
    try {
      assert.ok(store instanceof Store);
      assert.ok(lines.join("").includes(`store_max_memory is not enforced on ${platform}`), lines.join(""));
    } finally {
      await store.close();
    }
  });
}

test("the Linux sampler reads VmRSS in kB, not pages", () => {
  assert.equal(parseStatusRss("Name:\tnode\nVmRSS:\t  123456 kB\nVmData:\t1 kB\n"), 123456 * 1024);
  assert.equal(parseStatusRss("Name:\tnode\n"), null);
});

test("the macOS sampler reads the physical footprint", { skip: process.platform !== "darwin" }, async () => {
  assert.equal(parseFootprint("node [1]: 64-bit    Footprint: 1233 MB (16384 bytes per page)"), 1233 * 1024 ** 2);
  assert.equal(parseFootprint("node [1]: 64-bit    Footprint: 1472 KB (16384 bytes per page)"), 1472 * 1024);
  const child = spawn(process.execPath, ["-e", "const b = Buffer.alloc(200 * 1024 * 1024, 1); setInterval(() => b[0]++, 100);"], { stdio: "ignore" });
  try {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const pid = child.pid ?? -1;
    const sampled = await memorySampler().sample(pid);
    const reported = parseFootprint(spawnSync("/usr/bin/footprint", ["-p", String(pid)], { encoding: "utf8" }).stdout);
    assert.ok(sampled !== null && reported !== null);
    assert.ok(sampled >= reported, `${sampled} < footprint ${reported}`);
  } finally {
    child.kill("SIGKILL");
  }
});
