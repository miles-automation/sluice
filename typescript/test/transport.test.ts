import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { after, describe, test } from "node:test";
import { Client, type CallToolResult } from "@modelcontextprotocol/client";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import { DownstreamError, FailureClass } from "../src/errors.ts";
import { Interceptor } from "../src/intercept.ts";
import { dumpJson } from "../src/json.ts";
import { mountedName } from "../src/naming.ts";
import type { FinalResult } from "../src/paginate.ts";
import { Proxy } from "../src/proxy.ts";
import { QueryTool } from "../src/query.ts";
import { buildServer } from "../src/server.ts";
import { Store } from "../src/store.ts";
import { DownstreamTransport, UpstreamTransport } from "../src/transport.ts";
import { CLI, FAKE_SERVER, PACKAGE_ROOT, RAW_SERVER, fakeConfig, rawConfig, sqlRows, text, withProxy, withSluice } from "./support.ts";

const raw = (tool: string): string => mountedName("raw", tool);
const directory = mkdtempSync(join(tmpdir(), "sluice-transport-"));
after(() => rmSync(directory, { recursive: true, force: true }));

describe("downstream transport", () => {
  test("only the first response for an in-flight id is accepted, and its raw text is the one recorded", () =>
    withProxy(rawConfig(), async (proxy) => {
      const reply = await proxy.call(raw("duplicate"), { arguments: null });
      assert.equal(reply.kind, "result");
      const { result, structured } = reply as FinalResult;
      assert.equal(text(result as CallToolResult), "first");
      assert.deepEqual(result.structuredContent, { v: 1 });
      assert.ok(structured !== undefined && structured !== null);
      assert.equal(dumpJson(structured), '{"v": 1}');
    }));

  test("a second response for the same id never reaches the table", () =>
    withSluice(rawConfig(), async ({ client, store }) => {
      const result = await client.callTool({ name: raw("duplicate") });
      assert.notEqual(result.isError, true);
      const rows = await sqlRows(store, "SELECT result_structured FROM sluice_calls");
      assert.equal(rows.length, 1);
      assert.ok(!String(rows[0]?.[0]).includes("INJECTED"), String(rows[0]?.[0]));
      assert.ok(!JSON.stringify(result).includes("INJECTED"));
    }));

  test("a response for an id that is not in flight is dropped", () =>
    withProxy(rawConfig(), async (proxy) => {
      const reply = await proxy.call(raw("stray"), { arguments: null });
      assert.equal(reply.kind, "result");
      assert.equal(text((reply as FinalResult).result as CallToolResult), '{"items": [{"ok": true}]}');
    }));

  test("a malformed response line fails its call promptly as a protocol error", { timeout: 10_000 }, () =>
    withProxy(rawConfig(), async (proxy) => {
      const started = Date.now();
      await assert.rejects(proxy.call(raw("malformed"), { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.PROTOCOL);
        assert.match(error.message, /malformed response/u);
        return true;
      });
      assert.ok(Date.now() - started < 5000);
    }));

  test("a cancelled call leaves nothing in flight", async () => {
    const transport = new DownstreamTransport({ command: process.execPath, args: [FAKE_SERVER], env: {}, cwd: PACKAGE_ROOT });
    const client = new Client({ name: "t", version: "0" }, { versionNegotiation: { mode: "auto" } });
    await client.connect(transport);
    try {
      const controller = new AbortController();
      const pending = client.callTool({ name: "slow", arguments: { delay_ms: 2000 } }, { signal: controller.signal });
      await new Promise((resolve) => setTimeout(resolve, 100));
      assert.equal(transport.inFlight, 1);
      controller.abort();
      await assert.rejects(pending);
      assert.equal(transport.inFlight, 0);
    } finally {
      await client.close();
    }
  });

  test("debug logging names a dropped response by id, never by its payload", async () => {
    const config = join(directory, "raw.toml");
    writeFileSync(config, `[servers.raw]\ncommand = ${JSON.stringify(process.execPath)}\nargs = [${JSON.stringify(RAW_SERVER)}]\n`, "utf8");
    const child = execFile(process.execPath, [CLI, "--config", config, "--log-level", "debug"], { cwd: PACKAGE_ROOT });
    const stderr: string[] = [];
    child.stderr?.on("data", (chunk: Buffer) => stderr.push(chunk.toString("utf8")));
    const exited = new Promise((resolve) => child.once("exit", resolve));
    const lines: string[] = [];
    let buffer = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const parts = buffer.split("\n");
      buffer = parts.pop() ?? "";
      lines.push(...parts);
    });
    const waitFor = async (id: number): Promise<void> => {
      const started = Date.now();
      while (!lines.some((line) => (JSON.parse(line) as { id?: unknown }).id === id)) {
        assert.ok(Date.now() - started < 20_000, `no response ${id}`);
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    };
    child.stdin?.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "t", version: "0" } } })}\n`);
    await waitFor(1);
    child.stdin?.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);
    child.stdin?.write(`${JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: raw("stray") } })}\n`);
    await waitFor(2);
    child.stdin?.end();
    await exited;
    const logs = stderr.join("");
    assert.match(logs, /dropped a response for id 987654/u);
    assert.ok(!logs.includes('"text":"stray"'), logs);
  });
});

describe("upstream transport", () => {
  test("a request id already in flight is refused, and the first call keeps its own arguments", async () => {
    const config = fakeConfig();
    const proxy = await Proxy.start(config);
    const store = await Store.open(config.limits);
    const toServer = new PassThrough();
    const toClient = new PassThrough();
    const upstream = new UpstreamTransport(toServer, toClient);
    const handle = serveStdio(
      () =>
        buildServer({
          proxy,
          interceptor: new Interceptor(store, config.limits, { queryAvailable: true }),
          query: new QueryTool(store, config.limits),
          requests: (id) => upstream.takeRequest(id),
        }),
      { transport: upstream },
    );
    const responses: Record<string, unknown>[] = [];
    let buffer = "";
    toClient.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const parts = buffer.split("\n");
      buffer = parts.pop() ?? "";
      responses.push(...parts.map((line) => JSON.parse(line) as Record<string, unknown>));
    });
    const until = async (count: number): Promise<void> => {
      const started = Date.now();
      while (responses.length < count) {
        assert.ok(Date.now() - started < 10_000, `only ${responses.length} responses`);
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    };
    try {
      toServer.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "t", version: "0" } } })}\n`);
      await until(1);
      toServer.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);
      const name = mountedName("fake", "slow_echo");
      toServer.write(`${JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/call", params: { name, arguments: { delay_ms: 400, tag: "first" } } })}\n`);
      await new Promise((resolve) => setTimeout(resolve, 100));
      toServer.write(`${JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/call", params: { name, arguments: { delay_ms: 0, tag: "second" } } })}\n`);
      await until(3);
      await new Promise((resolve) => setTimeout(resolve, 300));
      const answers = responses.slice(1);
      assert.equal(answers.length, 2, JSON.stringify(answers));
      const refused = answers.filter((answer) => answer["error"] !== undefined);
      assert.equal(refused.length, 1);
      assert.equal((refused[0]?.["error"] as Record<string, unknown>)["code"], -32600);
      const rows = await sqlRows(store, "SELECT args FROM sluice_calls");
      assert.deepEqual(rows, [['{"delay_ms": 400, "tag": "first"}']]);
      assert.equal(upstream.inFlight, 0);
    } finally {
      await handle.close();
      store.close();
      await proxy.close();
    }
  });

  test("a cancelled upstream request is no longer tracked", () =>
    withSluice(fakeConfig(), async ({ client, upstream }) => {
      const controller = new AbortController();
      const pending = client.callTool({ name: mountedName("fake", "slow"), arguments: { delay_ms: 2000 } }, { signal: controller.signal });
      await new Promise((resolve) => setTimeout(resolve, 200));
      controller.abort();
      await assert.rejects(pending);
      await new Promise((resolve) => setTimeout(resolve, 100));
      assert.equal(upstream.inFlight, 0);
      const again = await client.callTool({ name: mountedName("fake", "rows"), arguments: { n: 2 } });
      assert.notEqual(again.isError, true);
    }));
});

