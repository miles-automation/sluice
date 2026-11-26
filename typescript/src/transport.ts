import { AsyncLocalStorage } from "node:async_hooks";
import { spawn, type ChildProcess } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { parseJSONRPCMessage, type JSONRPCMessage, type Transport } from "@modelcontextprotocol/client";
import { getDefaultEnvironment } from "@modelcontextprotocol/client/stdio";
import { isJsonObject, parseJson, type JsonObject, type JsonValue } from "./json.ts";

export interface Capture {
  raw: JsonObject | undefined;
}

export const captures = new AsyncLocalStorage<Capture>();

export function capturing<T>(work: (capture: Capture) => Promise<T>): Promise<T> {
  const capture: Capture = { raw: undefined };
  return captures.run(capture, () => work(capture));
}

type Id = string | number | bigint;

export function idKey(id: Id): string {
  return typeof id === "string" ? `s:${id}` : `n:${String(id)}`;
}

function field(message: unknown, name: string): unknown {
  return typeof message === "object" && message !== null ? (message as Record<string, unknown>)[name] : undefined;
}

function messageId(message: unknown): Id | undefined {
  const id = field(message, "id");
  return typeof id === "string" || typeof id === "number" ? id : undefined;
}

function lossless(line: string): JsonObject | undefined {
  try {
    const value = parseJson(line);
    return isJsonObject(value) ? value : undefined;
  } catch {
    return undefined;
  }
}

class LineSplitter {
  #pending: Buffer[] = [];

  push(chunk: Buffer, emit: (line: string) => void): void {
    let start = 0;
    for (let index = chunk.indexOf(10); index !== -1; index = chunk.indexOf(10, start)) {
      this.#pending.push(chunk.subarray(start, index));
      const line = Buffer.concat(this.#pending).toString("utf8").replace(/\r$/u, "");
      this.#pending = [];
      start = index + 1;
      if (line.trim() !== "") {
        emit(line);
      }
    }
    if (start < chunk.length) {
      this.#pending.push(chunk.subarray(start));
    }
  }

  clear(): void {
    this.#pending = [];
  }
}

function decode(line: string, onerror: ((error: Error) => void) | undefined): JSONRPCMessage | undefined {
  let plain: unknown;
  try {
    plain = JSON.parse(line);
  } catch (error) {
    onerror?.(error instanceof Error ? error : new Error(String(error)));
    return undefined;
  }
  try {
    return parseJSONRPCMessage(plain);
  } catch (error) {
    onerror?.(error instanceof Error ? error : new Error(String(error)));
    return undefined;
  }
}

export interface ChildParameters {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly cwd: string | null;
}

const CAPTURED_METHODS: ReadonlySet<string> = new Set(["tools/call"]);

export class DownstreamTransport implements Transport {
  readonly #parameters: ChildParameters;
  readonly #lines = new LineSplitter();
  readonly #pending = new Map<string, Capture>();
  #process: ChildProcess | undefined;
  onclose?: (() => void) | undefined;
  onerror?: ((error: Error) => void) | undefined;
  onmessage?: (<T extends JSONRPCMessage>(message: T) => void) | undefined;

  constructor(parameters: ChildParameters) {
    this.#parameters = parameters;
  }

  get pid(): number | undefined {
    return this.#process?.pid;
  }

  start(): Promise<void> {
    if (this.#process !== undefined) {
      throw new Error("the downstream transport is already started");
    }
    return new Promise((resolve, reject) => {
      const child = spawn(this.#parameters.command, [...this.#parameters.args], {
        env: { ...getDefaultEnvironment(), ...this.#parameters.env },
        stdio: ["pipe", "pipe", "inherit"],
        shell: false,
        ...(this.#parameters.cwd === null ? {} : { cwd: this.#parameters.cwd }),
      });
      this.#process = child;
      child.once("error", (error) => {
        reject(error);
        this.onerror?.(error);
      });
      child.once("spawn", () => resolve());
      child.once("close", () => {
        this.#process = undefined;
        this.#pending.clear();
        this.onclose?.();
      });
      child.stdin?.on("error", (error) => this.onerror?.(error));
      child.stdout?.on("error", (error) => this.onerror?.(error));
      child.stdout?.on("data", (chunk: Buffer) => this.#lines.push(chunk, (line) => this.#receive(line)));
    });
  }

  #receive(line: string): void {
    const message = decode(line, this.onerror);
    if (message === undefined) {
      return;
    }
    const id = messageId(message);
    if (id !== undefined && field(message, "method") === undefined) {
      const capture = this.#pending.get(idKey(id));
      if (capture !== undefined) {
        this.#pending.delete(idKey(id));
        const parsed = lossless(line);
        const result = parsed?.get("result");
        capture.raw = isJsonObject(result) ? result : undefined;
      }
    }
    this.onmessage?.(message);
  }

  send(message: JSONRPCMessage): Promise<void> {
    const stdin = this.#process?.stdin;
    if (stdin === undefined || stdin === null) {
      return Promise.reject(new Error("the downstream transport is not connected"));
    }
    const capture = captures.getStore();
    const id = messageId(message);
    const method = field(message, "method");
    if (capture !== undefined && id !== undefined && typeof method === "string" && CAPTURED_METHODS.has(method)) {
      this.#pending.set(idKey(id), capture);
    }
    return new Promise((resolve, reject) => {
      const ok = stdin.write(`${JSON.stringify(message)}\n`, (error) => (error ? reject(error) : undefined));
      if (ok) {
        resolve();
      } else {
        stdin.once("drain", resolve);
      }
    });
  }

  async close(): Promise<void> {
    const child = this.#process;
    this.#lines.clear();
    if (child === undefined) {
      return;
    }
    this.#process = undefined;
    const closed = new Promise<void>((resolve) => child.once("close", () => resolve()));
    const settle = (ms: number): Promise<void> =>
      Promise.race([closed, new Promise<void>((resolve) => setTimeout(resolve, ms).unref())]);
    child.stdin?.end();
    await settle(2000);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      await settle(2000);
    }
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
    }
  }
}

export class UpstreamTransport implements Transport {
  readonly #stdin: Readable;
  readonly #stdout: Writable;
  readonly #lines = new LineSplitter();
  readonly #requests = new Map<string, JsonObject>();
  #started = false;
  #closed = false;
  #resolveClosed!: () => void;
  readonly closed: Promise<void>;
  onclose?: (() => void) | undefined;
  onerror?: ((error: Error) => void) | undefined;
  onmessage?: (<T extends JSONRPCMessage>(message: T) => void) | undefined;

  constructor(stdin: Readable = process.stdin, stdout: Writable = process.stdout) {
    this.#stdin = stdin;
    this.#stdout = stdout;
    this.closed = new Promise((resolve) => {
      this.#resolveClosed = resolve;
    });
  }

  readonly #ondata = (chunk: Buffer): void => {
    this.#lines.push(chunk, (line) => this.#receive(line));
  };

  readonly #onend = (): void => {
    void this.close();
  };

  readonly #onerror = (error: Error): void => {
    this.onerror?.(error);
  };

  start(): Promise<void> {
    if (this.#started) {
      throw new Error("the upstream transport is already started");
    }
    this.#started = true;
    this.#stdin.on("data", this.#ondata);
    this.#stdin.on("end", this.#onend);
    this.#stdin.on("error", this.#onerror);
    this.#stdout.on("error", this.#onerror);
    return Promise.resolve();
  }

  takeRequest(id: Id): JsonObject | undefined {
    const key = idKey(id);
    const params = this.#requests.get(key);
    this.#requests.delete(key);
    return params;
  }

  #receive(line: string): void {
    const message = decode(line, this.onerror);
    if (message === undefined) {
      return;
    }
    const id = messageId(message);
    if (id !== undefined && field(message, "method") === "tools/call") {
      const params: JsonValue | undefined = lossless(line)?.get("params");
      if (isJsonObject(params)) {
        this.#requests.set(idKey(id), params);
      }
    }
    this.onmessage?.(message);
  }

  send(message: JSONRPCMessage): Promise<void> {
    const id = messageId(message);
    if (id !== undefined && field(message, "method") === undefined) {
      this.#requests.delete(idKey(id));
    }
    return new Promise((resolve) => {
      if (this.#stdout.write(`${JSON.stringify(message)}\n`)) {
        resolve();
      } else {
        this.#stdout.once("drain", resolve);
      }
    });
  }

  close(): Promise<void> {
    if (this.#closed) {
      return Promise.resolve();
    }
    this.#closed = true;
    this.#stdin.off("data", this.#ondata);
    this.#stdin.off("end", this.#onend);
    this.#stdin.off("error", this.#onerror);
    this.#stdin.pause();
    this.#lines.clear();
    this.#requests.clear();
    this.onclose?.();
    this.#resolveClosed();
    return Promise.resolve();
  }
}
