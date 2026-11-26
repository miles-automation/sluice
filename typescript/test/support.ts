import { PassThrough, type Readable, type Writable } from "node:stream";
import { fileURLToPath } from "node:url";
import { Client, parseJSONRPCMessage, type CallToolResult, type JSONRPCMessage, type Transport } from "@modelcontextprotocol/client";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import type { Config, PaginationConfig } from "../src/config.ts";
import { Interceptor } from "../src/intercept.ts";
import { DEFAULT_LIMITS, type Limits } from "../src/limits.ts";
import { Proxy } from "../src/proxy.ts";
import { QueryTool } from "../src/query.ts";
import { buildServer } from "../src/server.ts";
import { Store } from "../src/store.ts";
import { UpstreamTransport } from "../src/transport.ts";

export const PACKAGE_ROOT = fileURLToPath(new URL("../", import.meta.url));
export const FAKE_SERVER = fileURLToPath(new URL("fixtures/fake-server.ts", import.meta.url));
export const CLI = fileURLToPath(new URL("../src/cli.ts", import.meta.url));

export function fakeConfig(
  options: { readonly pagination?: readonly PaginationConfig[]; readonly limits?: Limits; readonly legacy?: boolean } = {},
): Config {
  return {
    server: {
      name: "fake",
      command: process.execPath,
      args: options.legacy === true ? [FAKE_SERVER, "--legacy"] : [FAKE_SERVER],
      env: {},
      cwd: PACKAGE_ROOT,
    },
    limits: options.limits ?? DEFAULT_LIMITS,
    pagination: new Map((options.pagination ?? []).map((entry) => [entry.tool, entry])),
  };
}

export async function withProxy<T>(config: Config, work: (proxy: Proxy) => Promise<T>): Promise<T> {
  const proxy = await Proxy.start(config);
  try {
    return await work(proxy);
  } finally {
    await proxy.close();
  }
}

export class StreamClientTransport implements Transport {
  readonly #input: Readable;
  readonly #output: Writable;
  #buffer = "";
  onclose?: (() => void) | undefined;
  onerror?: ((error: Error) => void) | undefined;
  onmessage?: (<T extends JSONRPCMessage>(message: T) => void) | undefined;
  readonly lines: string[] = [];

  constructor(input: Readable, output: Writable) {
    this.#input = input;
    this.#output = output;
  }

  start(): Promise<void> {
    this.#input.on("data", (chunk: Buffer) => {
      this.#buffer += chunk.toString("utf8");
      for (let index = this.#buffer.indexOf("\n"); index !== -1; index = this.#buffer.indexOf("\n")) {
        const line = this.#buffer.slice(0, index);
        this.#buffer = this.#buffer.slice(index + 1);
        this.lines.push(line);
        this.onmessage?.(parseJSONRPCMessage(JSON.parse(line)));
      }
    });
    return Promise.resolve();
  }

  send(message: JSONRPCMessage): Promise<void> {
    this.#output.write(`${JSON.stringify(message)}\n`);
    return Promise.resolve();
  }

  close(): Promise<void> {
    this.#output.end();
    this.onclose?.();
    return Promise.resolve();
  }
}

export interface Session {
  readonly client: Client;
  readonly proxy: Proxy;
  readonly store: Store;
  readonly wire: readonly string[];
}

export interface SessionOptions {
  readonly interceptor?: boolean;
  readonly query?: boolean;
  readonly mode?: "auto" | "legacy";
  readonly capabilities?: Record<string, unknown>;
}

export async function withSluice<T>(config: Config, work: (session: Session) => Promise<T>, options: SessionOptions = {}): Promise<T> {
  const proxy = await Proxy.start(config);
  const store = await Store.open(config.limits);
  const toServer = new PassThrough();
  const toClient = new PassThrough();
  const upstream = new UpstreamTransport(toServer, toClient);
  const interceptor = options.interceptor === false ? null : new Interceptor(store, config.limits, { queryAvailable: true });
  const query = options.query === false ? null : new QueryTool(store, config.limits);
  const handle = serveStdio(
    () => buildServer({ proxy, interceptor, query, requests: (id) => upstream.takeRequest(id) }),
    { transport: upstream },
  );
  const transport = new StreamClientTransport(toClient, toServer);
  const client = new Client(
    { name: "test-client", version: "0" },
    {
      versionNegotiation: { mode: options.mode ?? "auto" },
      inputRequired: { autoFulfill: false },
      capabilities: options.capabilities ?? {},
    },
  );
  try {
    await client.connect(transport);
    return await work({ client, proxy, store, wire: transport.lines });
  } finally {
    await client.close();
    await handle.close();
    store.close();
    await proxy.close();
  }
}

export function text(result: CallToolResult | { readonly content: readonly { readonly type: string; readonly text?: string }[] }): string {
  const [block] = result.content;
  return block !== undefined && block.type === "text" && typeof block.text === "string" ? block.text : "";
}

export function structured(result: { readonly structuredContent?: unknown }): Record<string, unknown> {
  const value = result.structuredContent;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("expected structured content");
  }
  return value as Record<string, unknown>;
}

export function tables(result: { readonly structuredContent?: unknown }): Record<string, unknown>[] {
  return structured(result)["tables"] as Record<string, unknown>[];
}

export function tableName(result: { readonly structuredContent?: unknown }): string {
  const listed = tables(result);
  if (listed.length !== 1) {
    throw new Error(`expected one table, got ${listed.length}`);
  }
  return String(listed[0]?.["name"]);
}

export async function sqlRows(store: Store, sql: string): Promise<unknown[][]> {
  const connection = await store.connect();
  try {
    return (await connection.runAndReadAll(sql)).getRows();
  } finally {
    connection.closeSync();
  }
}
