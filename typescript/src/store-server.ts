import { readFileSync, writeSync } from "node:fs";
import { Socket } from "node:net";
import { AllocationFailed, QueryInterrupted, executeOnConnection, isAllocationFailure } from "./execute.ts";
import { QueryRejectedError } from "./gate.ts";
import { BREACH_EXIT, FrameReader, REPLY_MAX_BYTES, frame, type Message } from "./ipc.ts";
import { makeLimits } from "./limits.ts";
import type { CallRecord, TablePlan } from "./models.ts";
import { Store } from "./store.ts";
import { Mutex } from "./sync.ts";

export const SELF_SAMPLE_MS = 20;

function describe(error: unknown): { readonly error: string; readonly message: string } {
  return error instanceof Error ? { error: error.name, message: error.message } : { error: "Error", message: String(error) };
}

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function dataSegmentBytes(): number | null {
  if (process.platform !== "linux") {
    return null;
  }
  try {
    const match = /^VmData:\s+([0-9]+)\s+kB$/mu.exec(readFileSync("/proc/self/status", "ascii"));
    return match?.[1] === undefined ? null : Number(match[1]) * 1024;
  } catch {
    return null;
  }
}

export function replyFrame(message: Message, maxBytes: number = REPLY_MAX_BYTES): Buffer {
  const encoded = frame(message);
  if (encoded.length <= maxBytes) {
    return encoded;
  }
  return frame({
    id: message["id"],
    ok: false,
    kind: "rejected",
    error: "ReplyTooLarge",
    message: `the result is ${encoded.length} bytes, over the ${maxBytes} byte limit for one reply from the store; select fewer or narrower columns`,
  });
}

class StoreServer {
  readonly #descriptor: number;
  readonly #socket: Socket;
  #cap = Number.POSITIVE_INFINITY;
  readonly #writes = new Mutex();
  readonly #queries = new Map<number, AbortController>();
  #store: Store | undefined;
  #busy = 0;
  #watch: ReturnType<typeof setInterval> | undefined;

  constructor(descriptor: number) {
    this.#descriptor = descriptor;
    this.#socket = new Socket({ fd: descriptor, readable: true, writable: true });
  }

  serve(): void {
    const reader = new FrameReader();
    this.#socket.on("data", (chunk: Buffer) => {
      let messages: Message[];
      try {
        messages = reader.push(chunk);
      } catch (error) {
        process.stderr.write(`sluice store: unreadable frame: ${describe(error).message}\n`);
        process.exit(1);
      }
      for (const message of messages) {
        this.#dispatch(message);
      }
    });
    this.#socket.on("close", () => process.exit(0));
    this.#socket.on("error", () => process.exit(0));
  }

  #send(message: Message): void {
    this.#socket.write(replyFrame(message));
  }

  breach(detail: string): never {
    try {
      writeSync(this.#descriptor, frame({ breach: detail }));
    } catch {
      process.stderr.write(`sluice store: ${detail}\n`);
    }
    process.kill(process.pid, "SIGKILL");
    process.exit(BREACH_EXIT);
  }

  #enter(): void {
    this.#busy += 1;
    if (this.#watch === undefined) {
      this.#watch = setInterval(() => {
        const used = process.memoryUsage.rss();
        if (used > this.#cap) {
          this.breach(`the store process reached ${mib(used)}, over its memory cap, and was killed`);
        }
      }, SELF_SAMPLE_MS);
    }
  }

  #leave(): void {
    this.#busy -= 1;
    if (this.#busy === 0 && this.#watch !== undefined) {
      clearInterval(this.#watch);
      this.#watch = undefined;
    }
  }

  #dispatch(message: Message): void {
    const id = Number(message["id"] ?? 0);
    switch (message["op"]) {
      case "open":
        void this.#open(message);
        return;
      case "interrupt":
        this.#queries.get(Number(message["target"]))?.abort();
        return;
      case "commit":
        void this.#tracked(() => this.#commit(id, message));
        return;
      case "query": {
        const control = new AbortController();
        this.#queries.set(id, control);
        void this.#tracked(() => this.#query(id, message, control.signal)).finally(() => this.#queries.delete(id));
        return;
      }
      default:
        this.#send({ id, ok: false, error: "ProtocolError", message: `unknown operation ${String(message["op"])}` });
    }
  }

  async #tracked(work: () => Promise<void>): Promise<void> {
    this.#enter();
    try {
      await work();
    } finally {
      this.#leave();
    }
  }

  #require(): Store {
    if (this.#store === undefined) {
      throw new Error("the store was not opened");
    }
    return this.#store;
  }

  async #open(message: Message): Promise<void> {
    try {
      const limits = makeLimits(message["limits"] as Record<string, unknown>);
      this.#cap = limits.store_max_memory;
      this.#store = await Store.open(limits, Number(message["generation"]));
      this.#send({ id: 0, ok: true, data_segment: dataSegmentBytes() });
    } catch (error) {
      this.#send({ id: 0, ok: false, ...describe(error) });
    }
  }

  async #commit(id: number, message: Message): Promise<void> {
    try {
      const store = this.#require();
      const outcome = await this.#writes.run(() =>
        store.commitNow(message["record"] as CallRecord, message["plans"] as readonly TablePlan[]),
      );
      this.#send({ id, ok: true, retained: outcome.retained, envelope: outcome.envelope, evicted: outcome.evicted ?? [] });
    } catch (error) {
      if (isAllocationFailure(error)) {
        this.breach(`an allocation failed inside the store (${describe(error).message})`);
      }
      this.#send({ id, ok: false, ...describe(error) });
    }
  }

  async #query(id: number, message: Message, signal: AbortSignal): Promise<void> {
    try {
      const rows = await executeOnConnection(
        this.#require(),
        String(message["sql"]),
        Number(message["limit"]),
        Number(message["cell_bytes"]),
        Number(message["output_bytes"]),
        signal,
      );
      this.#send({ id, ok: true, columns: rows.columns, rows: rows.rows, more_rows: rows.moreRows, omitted: rows.omitted });
    } catch (error) {
      if (error instanceof AllocationFailed || isAllocationFailure(error)) {
        this.breach(`an allocation failed inside the store (${describe(error).message})`);
      }
      if (error instanceof QueryInterrupted) {
        this.#send({ id, ok: false, kind: "interrupted" });
      } else if (error instanceof QueryRejectedError) {
        this.#send({ id, ok: false, kind: "rejected", message: error.message });
      } else {
        const { error: name, message: text } = describe(error);
        this.#send({ id, ok: false, kind: "rejected", message: `${name}: ${text}` });
      }
    }
  }
}

export function serveStore(descriptor: number): void {
  new StoreServer(descriptor).serve();
}
