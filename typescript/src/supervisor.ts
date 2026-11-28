import { execFile, spawn, type ChildProcess } from "node:child_process";
import { readFile } from "node:fs/promises";
import type { Socket } from "node:net";
import { fileURLToPath } from "node:url";
import { INTERRUPT_GRACE_MS, timeoutFailure, type QueryRows } from "./execute.ts";
import { QueryRejectedError } from "./gate.ts";
import { BREACH_EXIT, FrameReader, REPLY_MAX_BYTES, frame, type Message } from "./ipc.ts";
import type { Limits } from "./limits.ts";
import { logger } from "./log.ts";
import type { CallRecord, TablePlan } from "./models.ts";
import { CommitSequencer, Store, type CommitOutcome } from "./store.ts";
import { longTimeout } from "./timer.ts";

const log = logger("sluice.supervisor");

export const SAMPLE_INTERVAL_MS = 20;
export const PS_SAMPLE_INTERVAL_MS = 50;
export const OPEN_TIMEOUT_MS = 60_000;
export const CLOSE_TIMEOUT_MS = 5000;

const TOKEN = /[a-z0-9_]+/gu;

export const DATA_MARGIN_BYTES = 64 * 1024 * 1024;

export function dataLimit(baseline: number, cap: number): number {
  return baseline + cap + DATA_MARGIN_BYTES;
}

export type Sampler = (pid: number) => Promise<number | null>;

const STATUS_RSS = /^VmRSS:\s+([0-9]+)\s+kB$/mu;
const FOOTPRINT = /Footprint:\s+([0-9.]+)\s+(B|KB|MB|GB|TB)\b/u;
const FOOTPRINT_UNITS: Readonly<Record<string, number>> = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3, TB: 1024 ** 4 };

export function parseStatusRss(text: string): number | null {
  const match = STATUS_RSS.exec(text);
  return match?.[1] === undefined ? null : Number(match[1]) * 1024;
}

export function parseFootprint(text: string): number | null {
  const match = FOOTPRINT.exec(text);
  const unit = FOOTPRINT_UNITS[match?.[2] ?? ""];
  return match?.[1] === undefined || unit === undefined ? null : Math.round(Number(match[1]) * unit);
}

async function procSample(pid: number): Promise<number | null> {
  try {
    return parseStatusRss(await readFile(`/proc/${pid}/status`, "ascii"));
  } catch {
    return null;
  }
}

export const SAMPLE_COMMAND_TIMEOUT_MS = 1000;
export const FOOTPRINT_STALE_MS = 1000;
export const FOOTPRINT_PATH = "/usr/bin/footprint";

function run(command: string, args: readonly string[]): Promise<string | null> {
  return new Promise((resolve) => {
    execFile(
      command,
      [...args],
      { encoding: "utf8", timeout: SAMPLE_COMMAND_TIMEOUT_MS, killSignal: "SIGKILL" },
      (error, stdout) => {
        resolve(error === null ? stdout : null);
      },
    );
  });
}

async function psSample(pid: number): Promise<number | null> {
  const stdout = await run("ps", ["-o", "rss=", "-p", String(pid)]);
  const kib = Number(stdout?.trim() ?? "");
  return stdout !== null && stdout.trim() !== "" && Number.isFinite(kib) ? kib * 1024 : null;
}

export function footprintSampler(command: string = FOOTPRINT_PATH): Sampler {
  let latest: { readonly pid: number; readonly bytes: number; readonly at: number } | null = null;
  let running = false;
  const refresh = (pid: number): void => {
    if (running) {
      return;
    }
    running = true;
    void run(command, ["-p", String(pid)]).then((output) => {
      running = false;
      const bytes = output === null ? null : parseFootprint(output);
      if (bytes !== null) {
        latest = { pid, bytes, at: performance.now() };
      }
    });
  };
  return async (pid) => {
    refresh(pid);
    const resident = await psSample(pid);
    const physical =
      latest !== null && latest.pid === pid && performance.now() - latest.at < FOOTPRINT_STALE_MS ? latest.bytes : null;
    if (physical === null) {
      return resident;
    }
    return Math.max(physical, resident ?? 0);
  };
}

export function memorySampler(): { readonly sample: Sampler; readonly intervalMs: number } {
  if (process.platform === "linux") {
    return { sample: procSample, intervalMs: SAMPLE_INTERVAL_MS };
  }
  if (process.platform === "darwin") {
    return { sample: footprintSampler(), intervalMs: PS_SAMPLE_INTERVAL_MS };
  }
  return { sample: psSample, intervalMs: PS_SAMPLE_INTERVAL_MS };
}

export function describeBytes(value: number): string {
  for (const [unit, size] of [
    ["GiB", 1024 ** 3],
    ["MiB", 1024 ** 2],
    ["KiB", 1024],
  ] as const) {
    if (value >= size) {
      const amount = value / size;
      return Number.isInteger(amount) ? `${amount} ${unit}` : `${amount.toFixed(1)} ${unit}`;
    }
  }
  return `${value} B`;
}

export function resetMessage(cause: string, limits: Limits): string {
  return (
    `store reset: ${cause}. The store process is capped at store_max_memory = ` +
    `${limits.store_max_memory} bytes (${describeBytes(limits.store_max_memory)}); it was ` +
    "restarted, so every table and handle from before the reset is gone. Rerun the " +
    "source tool calls to materialize their results again."
  );
}

export function evictedMessage(name: string): string {
  return `table '${name}' was evicted by the session retention budget; rerun the source tool call to materialize it again`;
}

export function staleMessage(name: string, limits: Limits): string {
  return (
    `${name} was lost when the store was reset (store_max_memory = ` +
    `${describeBytes(limits.store_max_memory)}); its data is gone. Rerun the source tool call.`
  );
}

export class StoreDied extends Error {
  override readonly name = "StoreDied";
}

export class StoreResetError extends Error {
  override readonly name = "StoreResetError";
}

export class StoreWorkerError extends Error {
  override readonly name = "StoreWorkerError";
}

export class StoreStartError extends Error {
  override readonly name = "StoreStartError";
}

interface Pending {
  readonly resolve: (reply: Message) => void;
  readonly reject: (error: StoreDied) => void;
}

export interface Request {
  readonly id: number;
  readonly reply: Promise<Message>;
}

function workerCommand(): { readonly command: string; readonly args: string[] } {
  const extension = import.meta.url.endsWith(".ts") ? ".ts" : ".js";
  const worker = fileURLToPath(new URL(`./store-worker${extension}`, import.meta.url));
  return { command: process.execPath, args: [worker, "3"] };
}

let prlimitWarned = false;

async function limitData(pid: number, limit: number): Promise<number | null> {
  const applied = await run("prlimit", ["--pid", String(pid), `--data=${limit}:${limit}`]);
  if (applied !== null) {
    return limit;
  }
  if (!prlimitWarned) {
    prlimitWarned = true;
    log.warning("could not set RLIMIT_DATA on the store process (is util-linux prlimit installed?); the memory cap relies on sampling alone");
  }
  return null;
}

function exitText(code: number | null, signal: NodeJS.Signals | null): string {
  if (signal !== null) {
    return `signal ${signal}`;
  }
  if (code === BREACH_EXIT) {
    return "it reported an allocation failure";
  }
  return `exit status ${String(code)}`;
}

export class StoreProcess {
  readonly generation: number;
  readonly #limits: Limits;
  readonly #child: ChildProcess;
  readonly #socket: Socket;
  readonly #onExit: (process: StoreProcess, cause: string) => void;
  readonly #pending = new Map<number, Pending>();
  readonly #sampler = memorySampler();
  readonly #exited: Promise<void>;
  #opened: Pending | undefined;
  #nextId = 1;
  #cause: string | null = null;
  #breach: string | null = null;
  #closing = false;
  #dead = false;
  #sampling = false;
  #peakBytes = 0;
  #dataLimit: number | null = null;

  private constructor(limits: Limits, generation: number, onExit: (process: StoreProcess, cause: string) => void) {
    this.generation = generation;
    this.#limits = limits;
    this.#onExit = onExit;
    const { command, args } = workerCommand();
    this.#child = spawn(command, args, { stdio: ["ignore", 2, 2, "pipe"] });
    const socket = this.#child.stdio[3];
    if (socket === null || socket === undefined) {
      this.#child.kill("SIGKILL");
      throw new StoreStartError("the store process has no channel");
    }
    this.#socket = socket as Socket;
    const reader = new FrameReader(REPLY_MAX_BYTES);
    this.#socket.on("data", (chunk: Buffer) => {
      let messages: Message[];
      try {
        messages = reader.push(chunk);
      } catch {
        this.kill("the store process sent an unreadable reply");
        return;
      }
      for (const message of messages) {
        this.#receive(message);
      }
    });
    this.#socket.on("error", () => undefined);
    this.#exited = new Promise((resolve) => {
      this.#child.once("close", (code: number | null, signal: NodeJS.Signals | null) => {
        this.#finish(code, signal);
        resolve();
      });
    });
    this.#child.once("error", () => this.kill("the store process could not be started"));
  }

  static async start(
    limits: Limits,
    generation: number,
    onExit: (process: StoreProcess, cause: string) => void,
  ): Promise<StoreProcess> {
    const started = new StoreProcess(limits, generation, onExit);
    try {
      await started.#open();
    } catch (error) {
      started.#closing = true;
      started.#child.kill("SIGKILL");
      await started.#exited;
      throw error;
    }
    return started;
  }

  get pid(): number {
    return this.#child.pid ?? -1;
  }

  get alive(): boolean {
    return !this.#dead && this.#cause === null;
  }

  get peakBytes(): number {
    return this.#peakBytes;
  }

  get dataLimit(): number | null {
    return this.#dataLimit;
  }

  async #open(): Promise<void> {
    const reply = new Promise<Message>((resolve, reject) => {
      this.#opened = { resolve, reject };
    });
    const timer = setTimeout(() => this.kill("the store process did not open in time"), OPEN_TIMEOUT_MS);
    try {
      this.#socket.write(frame({ op: "open", generation: this.generation, limits: { ...this.#limits } }));
      const opened = await reply;
      if (opened["ok"] !== true) {
        throw new StoreStartError(`the store process failed to open: ${String(opened["error"])}: ${String(opened["message"])}`);
      }
      const baseline = opened["data_segment"];
      if (typeof baseline === "number") {
        this.#dataLimit = await limitData(this.pid, dataLimit(baseline, this.#limits.store_max_memory));
      }
    } catch (error) {
      if (error instanceof StoreDied) {
        throw new StoreStartError(`the store process failed to start (${error.message})`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
      this.#opened = undefined;
    }
  }

  #receive(message: Message): void {
    if ("breach" in message) {
      this.#breach = String(message["breach"]);
      return;
    }
    const id = Number(message["id"]);
    if (id === 0 && this.#opened !== undefined) {
      this.#opened.resolve(message);
      return;
    }
    const pending = this.#pending.get(id);
    if (pending !== undefined) {
      this.#pending.delete(id);
      pending.resolve(message);
    }
  }

  start(message: Message): Request {
    if (!this.alive) {
      const reply = Promise.reject(new StoreDied(this.#cause ?? "the store process has exited"));
      reply.catch(() => undefined);
      return { id: -1, reply };
    }
    const id = this.#nextId;
    this.#nextId += 1;
    const reply = new Promise<Message>((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
    });
    reply.catch(() => undefined);
    this.#socket.write(frame({ ...message, id }));
    this.#sample();
    return { id, reply };
  }

  request(message: Message): Promise<Message> {
    return this.start(message).reply;
  }

  notify(message: Message): void {
    if (this.alive) {
      this.#socket.write(frame(message));
    }
  }

  kill(cause: string): void {
    if (this.#dead || this.#cause !== null) {
      return;
    }
    this.#cause = cause;
    this.#child.kill("SIGKILL");
  }

  async close(): Promise<void> {
    this.#closing = true;
    if (!this.#dead) {
      this.#socket.end();
      const timer = setTimeout(() => this.#child.kill("SIGKILL"), CLOSE_TIMEOUT_MS);
      await this.#exited;
      clearTimeout(timer);
    }
  }

  #sample(): void {
    if (this.#sampling) {
      return;
    }
    this.#sampling = true;
    const loop = async (): Promise<void> => {
      while (this.#pending.size > 0 && this.alive) {
        const used = await this.#sampler.sample(this.pid);
        if (used !== null) {
          this.#peakBytes = Math.max(this.#peakBytes, used);
          if (used > this.#limits.store_max_memory) {
            this.kill(`the store process reached ${describeBytes(used)}, over its memory cap, and was killed`);
            break;
          }
        }
        await new Promise((resolve) => setTimeout(resolve, this.#sampler.intervalMs));
      }
      this.#sampling = false;
    };
    void loop();
  }

  #finish(code: number | null, signal: NodeJS.Signals | null): void {
    this.#dead = true;
    let cause: string;
    if (this.#closing) {
      cause = "the store was closed";
    } else if (this.#cause !== null) {
      cause = this.#cause;
    } else if (this.#breach !== null) {
      cause = this.#breach;
    } else {
      cause = `the store process exited unexpectedly (${exitText(code, signal)})`;
    }
    this.#cause = cause;
    if (!this.#closing) {
      log.error(`store process ${this.pid} ended: ${cause}`);
      this.#onExit(this, cause);
    }
    const failure = new StoreDied(cause);
    this.#opened?.reject(failure);
    for (const pending of this.#pending.values()) {
      pending.reject(failure);
    }
    this.#pending.clear();
  }
}

class BoundedNames {
  readonly #limit: number;
  readonly #names = new Set<string>();

  constructor(limit: number) {
    this.#limit = limit;
  }

  add(names: Iterable<string>): void {
    for (const name of names) {
      const folded = name.toLowerCase();
      this.#names.delete(folded);
      this.#names.add(folded);
    }
    while (this.#names.size > this.#limit) {
      const first = this.#names.values().next();
      if (first.done === true) {
        break;
      }
      this.#names.delete(first.value);
    }
  }

  take(): string[] {
    const names = [...this.#names];
    this.#names.clear();
    return names;
  }

  firstOf(tokens: Iterable<string>): string | null {
    for (const token of tokens) {
      if (this.#names.has(token)) {
        return token;
      }
    }
    return null;
  }
}

export class SupervisedStore extends CommitSequencer {
  readonly #limits: Limits;
  #process: StoreProcess | null = null;
  #spawning: Promise<StoreProcess> | null = null;
  #generation = 0;
  #resets = 0;
  #closed = false;
  readonly #known: BoundedNames;
  readonly #lost: BoundedNames;
  readonly #evicted: BoundedNames;
  readonly #lostEvicted: BoundedNames;

  private constructor(limits: Limits) {
    super();
    this.#limits = limits;
    this.#known = new BoundedNames(limits.max_session_calls * 64);
    this.#lost = new BoundedNames(limits.max_session_calls * 64);
    this.#evicted = new BoundedNames(limits.max_session_calls * 64);
    this.#lostEvicted = new BoundedNames(limits.max_session_calls * 64);
  }

  static async open(limits: Limits): Promise<SupervisedStore> {
    const store = new SupervisedStore(limits);
    await store.#ensure();
    return store;
  }

  get generation(): number {
    return this.#generation;
  }

  get resets(): number {
    return this.#resets;
  }

  get process(): StoreProcess | null {
    return this.#process;
  }

  async close(): Promise<void> {
    this.#closed = true;
    const spawning = this.#spawning;
    if (spawning !== null) {
      await spawning.catch(() => undefined);
    }
    const current = this.#process;
    this.#process = null;
    await current?.close();
  }

  #ensure(): Promise<StoreProcess> {
    if (this.#closed) {
      return Promise.reject(new Error("the store is closed"));
    }
    const current = this.#process;
    if (current !== null) {
      if (current.alive) {
        return Promise.resolve(current);
      }
      this.#exited(current);
    }
    if (this.#spawning === null) {
      const generation = this.#generation;
      this.#spawning = StoreProcess.start(this.#limits, generation, (ended) => this.#exited(ended))
        .then((started) => {
          this.#process = started;
          return started;
        })
        .finally(() => {
          this.#spawning = null;
        });
    }
    return this.#spawning;
  }

  #exited(ended: StoreProcess): void {
    if (ended !== this.#process) {
      return;
    }
    this.#process = null;
    this.#generation += 1;
    this.#resets += 1;
    this.#lost.add(this.#known.take());
    this.#lostEvicted.add(this.#evicted.take());
  }

  #remember(generation: number, names: readonly string[], evicted: readonly string[]): void {
    if (generation === this.#generation) {
      this.#known.add(names);
      this.#evicted.add(evicted);
    } else {
      this.#lost.add(names);
      this.#lostEvicted.add(evicted);
    }
  }

  #stale(sql: string): QueryRejectedError | null {
    const tokens = new Set(sql.toLowerCase().match(TOKEN) ?? []);
    const evicted = this.#lostEvicted.firstOf(tokens);
    if (evicted !== null) {
      return new QueryRejectedError(evictedMessage(evicted));
    }
    const lost = this.#lost.firstOf(tokens);
    return lost === null ? null : new QueryRejectedError(staleMessage(lost, this.#limits));
  }

  protected async write(record: CallRecord, plans: readonly TablePlan[]): Promise<CommitOutcome> {
    const process = await this.#ensure();
    let reply: Message;
    try {
      reply = await process.request({ op: "commit", record, plans });
    } catch (error) {
      if (error instanceof StoreDied) {
        throw new StoreResetError(resetMessage(error.message, this.#limits));
      }
      throw error;
    }
    if (reply["ok"] !== true) {
      throw new StoreWorkerError(`${String(reply["error"])}: ${String(reply["message"])}`);
    }
    const retained = reply["retained"] === true;
    const envelope = String(reply["envelope"]);
    const evicted = Array.isArray(reply["evicted"]) ? (reply["evicted"] as unknown[]).map(String) : [];
    this.#remember(process.generation, [...(retained ? plans.map((plan) => plan.name) : []), envelope], evicted);
    return { retained, envelope };
  }

  async runQuery(sql: string, limit: number, limits: Limits, deadline: number): Promise<QueryRows> {
    const stale = this.#stale(sql);
    if (stale !== null) {
      throw stale;
    }
    let process: StoreProcess;
    try {
      process = await this.#ensure();
    } catch (error) {
      throw new QueryRejectedError(`the store is unavailable: ${error instanceof Error ? error.message : String(error)}`);
    }
    const request = process.start({
      op: "query",
      sql,
      limit,
      cell_bytes: limits.max_cell_bytes,
      output_bytes: limits.query_max_bytes,
    });
    let expired = false;
    let grace: ReturnType<typeof setTimeout> | undefined;
    const cancelTimer = longTimeout(
      () => {
        expired = true;
        process.notify({ op: "interrupt", target: request.id });
        grace = setTimeout(
          () =>
            process.kill(
              `the query did not stop within ${INTERRUPT_GRACE_MS / 1000}s of its ${limits.query_timeout_seconds}s timeout interrupt`,
            ),
          INTERRUPT_GRACE_MS,
        );
      },
      Math.max(0, deadline - performance.now()),
    );
    let reply: Message;
    try {
      reply = await request.reply;
    } catch (error) {
      if (error instanceof StoreDied) {
        throw new QueryRejectedError(resetMessage(error.message, this.#limits));
      }
      throw error;
    } finally {
      cancelTimer();
      clearTimeout(grace);
    }
    if (reply["ok"] === true && !expired) {
      return {
        columns: reply["columns"] as string[],
        rows: reply["rows"] as (string | null)[][],
        moreRows: reply["more_rows"] === true,
        omitted: Number(reply["omitted"]),
      };
    }
    if (expired || reply["kind"] === "interrupted") {
      throw timeoutFailure(limits.query_timeout_seconds);
    }
    throw this.#stale(sql) ?? new QueryRejectedError(String(reply["message"]));
  }
}

export function supervisedPlatform(platform: string = process.platform): boolean {
  return platform === "darwin" || platform === "linux";
}

export async function openStore(limits: Limits, platform: string = process.platform): Promise<SupervisedStore | Store> {
  if (supervisedPlatform(platform)) {
    return SupervisedStore.open(limits);
  }
  log.warning(
    `store_max_memory is not enforced on ${platform}: the store process and its memory sampler run only on macOS and Linux, so DuckDB runs inside the Sluice process and a hostile query can use unbounded memory`,
  );
  return Store.open(limits);
}
