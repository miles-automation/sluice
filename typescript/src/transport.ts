import { AsyncLocalStorage } from "node:async_hooks";
import { spawn, type ChildProcess } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import { parseJSONRPCMessage, type JSONRPCMessage, type Transport } from "@modelcontextprotocol/client";
import { getDefaultEnvironment } from "@modelcontextprotocol/client/stdio";
import { isJsonObject, parseJson, type JsonObject, type JsonValue } from "./json.ts";

export class TransportNotice extends Error {
  override readonly name = "TransportNotice";
}

export function safeDescription(error: Error): string {
  if (error instanceof TransportNotice) {
    return error.message;
  }
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" || typeof code === "number" ? `${error.name} (${String(code)})` : error.name;
}

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

function describeId(id: unknown): string {
  return typeof id === "number" || typeof id === "string" ? JSON.stringify(id) : "none";
}

function decode(line: string, onerror: ((error: Error) => void) | undefined): JSONRPCMessage | undefined {
  let plain: unknown;
  try {
    plain = JSON.parse(line);
  } catch {
    onerror?.(new TransportNotice("received a line that is not JSON"));
    return undefined;
  }
  try {
    return parseJSONRPCMessage(plain);
  } catch {
    onerror?.(new TransportNotice(`received JSON that is not a JSON-RPC message (id ${describeId(field(plain, "id"))})`));
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
const CANCELLED = "notifications/cancelled";
const MALFORMED_RESPONSE = -32700;
const DUPLICATE_REQUEST = -32600;
const ID_IN_TEXT = /"id"\s*:\s*(-?[0-9]+)/u;

function responseKey(id: unknown): string | undefined {
  if (typeof id === "number") {
    return Number.isFinite(id) ? `n:${id}` : undefined;
  }
  if (typeof id !== "string") {
    return undefined;
  }
  const numeric = id.trim() === "" ? Number.NaN : Number(id);
  return Number.isFinite(numeric) ? `n:${numeric}` : `s:${id}`;
}

function cancelledId(message: unknown): unknown {
  if (field(message, "method") !== CANCELLED) {
    return undefined;
  }
  return field(field(message, "params"), "requestId");
}

function malformedId(line: string): unknown {
  try {
    return field(JSON.parse(line), "id");
  } catch {
    const found = ID_IN_TEXT.exec(line);
    return found === null ? undefined : Number(found[1]);
  }
}

export class DownstreamTransport implements Transport {
  readonly #parameters: ChildParameters;
  readonly #lines = new LineSplitter();
  readonly #inFlight = new Map<string, Capture | null>();
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

  get inFlight(): number {
    return this.#inFlight.size;
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
        this.#inFlight.clear();
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
      this.#failMalformed(line);
      return;
    }
    if (field(message, "method") === undefined && field(message, "id") !== undefined) {
      const key = responseKey(field(message, "id"));
      if (key === undefined || !this.#inFlight.has(key)) {
        this.onerror?.(new TransportNotice(`dropped a response for id ${describeId(field(message, "id"))}, which is not in flight`));
        return;
      }
      const capture = this.#inFlight.get(key) ?? null;
      this.#inFlight.delete(key);
      if (capture !== null) {
        const result = lossless(line)?.get("result");
        capture.raw = isJsonObject(result) ? result : undefined;
      }
    }
    this.onmessage?.(message);
  }

  #failMalformed(line: string): void {
    const key = responseKey(malformedId(line));
    if (key === undefined || !this.#inFlight.has(key)) {
      return;
    }
    this.#inFlight.delete(key);
    const original = malformedId(line);
    this.onmessage?.({
      jsonrpc: "2.0",
      id: typeof original === "string" || typeof original === "number" ? original : null,
      error: { code: MALFORMED_RESPONSE, message: "the downstream sent a malformed response to this request" },
    } as JSONRPCMessage);
  }

  send(message: JSONRPCMessage): Promise<void> {
    const stdin = this.#process?.stdin;
    if (stdin === undefined || stdin === null) {
      return Promise.reject(new Error("the downstream transport is not connected"));
    }
    const id = messageId(message);
    const method = field(message, "method");
    const key = responseKey(id);
    if (key !== undefined && typeof method === "string") {
      const capture = captures.getStore();
      this.#inFlight.set(key, capture !== undefined && CAPTURED_METHODS.has(method) ? capture : null);
    }
    const cancelled = responseKey(cancelledId(message));
    if (cancelled !== undefined) {
      this.#inFlight.delete(cancelled);
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

  terminate(): Promise<void> {
    return this.#stop(0);
  }

  close(): Promise<void> {
    return this.#stop(2000);
  }

  async #stop(grace: number): Promise<void> {
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
    if (grace > 0) {
      await settle(grace);
    }
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
  readonly #requests = new Map<string, JsonObject | null>();
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
    return this.#requests.get(idKey(id)) ?? undefined;
  }

  #receive(line: string): void {
    const message = decode(line, this.onerror);
    if (message === undefined) {
      return;
    }
    const id = messageId(message);
    const method = field(message, "method");
    if (id !== undefined && typeof method === "string") {
      const key = idKey(id);
      if (this.#requests.has(key)) {
        void this.#write({
          jsonrpc: "2.0",
          id,
          error: { code: DUPLICATE_REQUEST, message: `request id ${JSON.stringify(id)} is already in flight` },
        } as JSONRPCMessage);
        return;
      }
      const params: JsonValue | undefined = method === "tools/call" ? lossless(line)?.get("params") : undefined;
      this.#requests.set(key, isJsonObject(params) ? params : null);
    }
    const cancelled = cancelledId(message);
    if (typeof cancelled === "string" || typeof cancelled === "number") {
      this.#requests.delete(idKey(cancelled));
    }
    this.onmessage?.(message);
  }

  get inFlight(): number {
    return this.#requests.size;
  }

  send(message: JSONRPCMessage): Promise<void> {
    const id = messageId(message);
    if (id !== undefined && field(message, "method") === undefined) {
      this.#requests.delete(idKey(id));
    }
    return this.#write(message);
  }

  #write(message: JSONRPCMessage): Promise<void> {
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
