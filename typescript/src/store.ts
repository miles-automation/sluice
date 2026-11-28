import {
  BIGINT,
  BOOLEAN,
  DuckDBInstance,
  LIST,
  VARCHAR,
  listValue,
  type DuckDBAppender,
  type DuckDBConnection,
  type DuckDBType,
  type DuckDBValue,
} from "@duckdb/node-api";
import { ColumnType, coerce, type ColumnSpec } from "./infer.ts";
import { dumpJson, type JsonValue } from "./json.ts";
import { durationMs, type CallRecord, type SelectedPayload, type TablePlan } from "./models.ts";
import { quoteIdent } from "./naming.ts";
import { byteLength, describedValue } from "./payload.ts";
import { CALL_COLUMN, ROW_COLUMN } from "./shape.ts";
import { SCOPE_PATTERN, verifyParserContract } from "./gate.ts";
import type { Limits } from "./limits.ts";
import { Mutex } from "./sync.ts";
import { INTERRUPT_GRACE_MS, QueryInterrupted, executeOnConnection, timeoutFailure, type QueryRows } from "./execute.ts";
import { longTimeout } from "./timer.ts";

export const MAX_FLAT_REASON_BYTES = 512;
export const ENVELOPE_TABLE = "sluice_calls";

export function lockdownStatements(maxMemory: string): readonly string[] {
  return [
    "SET temp_directory = ''",
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    `SET max_memory = '${maxMemory.replaceAll("'", "''")}'`,
    "SET lock_configuration = true",
  ];
}

export function scopeViewName(scopeId: string, generation = 0): string {
  return generation === 0 ? `${ENVELOPE_TABLE}__${scopeId}` : `${ENVELOPE_TABLE}__${scopeId}__r${generation}`;
}

export function boundedFlatReason(reason: string | null): string | null {
  if (reason === null) {
    return null;
  }
  const encoded = new TextEncoder().encode(reason);
  if (encoded.length <= MAX_FLAT_REASON_BYTES) {
    return reason;
  }
  let cut = MAX_FLAT_REASON_BYTES;
  while (cut > 0 && ((encoded[cut] ?? 0) & 0xc0) === 0x80) {
    cut -= 1;
  }
  return new TextDecoder().decode(encoded.subarray(0, cut));
}

export function retentionBudgetReason(prior: string | null): string {
  const reason = prior ? `retention_budget_exceeded; prior=${prior}` : "retention_budget_exceeded";
  return boundedFlatReason(reason) || "retention_budget_exceeded";
}

const ENVELOPE_DDL = `
CREATE TABLE ${ENVELOPE_TABLE} (
    call_id           VARCHAR PRIMARY KEY,
    scope_id          VARCHAR,
    seq               BIGINT,
    server            VARCHAR,
    tool              VARCHAR,
    args              JSON,
    result            JSON,
    result_text       VARCHAR,
    result_blocks     JSON,
    result_structured JSON,
    source_channel    VARCHAR,
    channel_conflict  BOOLEAN,
    byte_size         BIGINT,
    wire_bytes        BIGINT,
    is_error          BOOLEAN,
    failure_class     VARCHAR,
    content_kinds     VARCHAR[],
    flat_tables       VARCHAR[],
    flat_reason       VARCHAR,
    source_paths      VARCHAR[],
    started_at        TIMESTAMP,
    ended_at          TIMESTAMP,
    duration_ms       BIGINT
)`;

const ENVELOPE_INSERT =
  `INSERT INTO ${ENVELOPE_TABLE} VALUES ($1, $2, $3, $4, $5, $6::JSON, $7::JSON, $8, $9::JSON, $10::JSON, ` +
  "$11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21::TIMESTAMP, $22::TIMESTAMP, $23)";

const ENVELOPE_TYPES: readonly DuckDBType[] = [
  VARCHAR,
  VARCHAR,
  BIGINT,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  VARCHAR,
  BOOLEAN,
  BIGINT,
  BIGINT,
  BOOLEAN,
  VARCHAR,
  LIST(VARCHAR),
  LIST(VARCHAR),
  VARCHAR,
  LIST(VARCHAR),
  VARCHAR,
  VARCHAR,
  BIGINT,
];

export function pythonDatetime(date: Date): string {
  const iso = date.toISOString();
  const base = `${iso.slice(0, 10)} ${iso.slice(11, 19)}`;
  const millis = date.getUTCMilliseconds();
  return millis === 0 ? base : `${base}.${String(millis).padStart(3, "0")}000`;
}

const JSON_ESCAPE = /\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})|\\u(d[89a-f][0-9a-f]{2})|\\./gu;

export function wellFormedJson(text: string): string {
  return text.replace(JSON_ESCAPE, (escape: string, _high?: string, _low?: string, lone?: string) =>
    lone === undefined ? escape : "\\ufffd",
  );
}

function storedBlocks(payload: SelectedPayload): JsonValue | null {
  if (payload.value === undefined && payload.text === null && payload.structured === null && payload.blocks.length === 0) {
    return null;
  }
  return describedValue(payload.blocks);
}

function jsonSize(value: JsonValue): number {
  return byteLength(dumpJson(value));
}

function strings(values: readonly string[]): JsonValue {
  return [...values];
}

function optionalBigInt(value: number | null): bigint | null {
  return value === null ? null : BigInt(value);
}

interface RetainedCall {
  readonly order: number;
  readonly size: number;
  readonly tables: readonly string[];
}

interface CallMetadata {
  readonly order: number;
  readonly scopeId: string;
  readonly tables: readonly string[];
}

function appendCell(appender: DuckDBAppender, value: JsonValue, column: ColumnSpec): void {
  const coerced = coerce(value, column.type);
  if (coerced === null) {
    appender.appendNull();
    return;
  }
  switch (column.type) {
    case ColumnType.BOOLEAN:
      appender.appendBoolean(coerced as boolean);
      return;
    case ColumnType.BIGINT:
      appender.appendBigInt(coerced as bigint);
      return;
    case ColumnType.HUGEINT:
      appender.appendHugeInt(coerced as bigint);
      return;
    case ColumnType.DOUBLE:
      appender.appendDouble(coerced as number);
      return;
    case ColumnType.VARCHAR:
    case ColumnType.JSON:
      appender.appendVarchar(String(coerced));
      return;
  }
}

function discard(appender: DuckDBAppender): void {
  try {
    appender.clear();
    appender.closeSync();
  } catch {
    return;
  }
}

type Waiter = () => void;

export interface CommitOutcome {
  readonly retained: boolean;
  readonly envelope: string;
  readonly evicted?: readonly string[];
}

export abstract class CommitSequencer {
  readonly #writeLock = new Mutex();
  #waiters: Waiter[] = [];
  #nextCommitSeq = 1;
  #retrySeq: number | null = null;
  readonly #abandoned = new Set<number>();
  readonly #callSequences = new Map<string, number>();
  readonly #tableSequences = new Map<string, number>();
  #retentionSequence = 0;

  protected abstract write(record: CallRecord, plans: readonly TablePlan[]): Promise<CommitOutcome>;

  abstract runQuery(sql: string, limit: number, limits: Limits, deadline: number): Promise<QueryRows>;

  protected get retentionSequence(): number {
    return this.#retentionSequence;
  }

  nextCallSeq(mounted: string): number {
    return CommitSequencer.#bump(this.#callSequences, mounted);
  }

  nextTableSeq(mounted: string): number {
    return CommitSequencer.#bump(this.#tableSequences, mounted);
  }

  nextRetentionSeq(): number {
    this.#retentionSequence += 1;
    return this.#retentionSequence;
  }

  static #bump(counters: Map<string, number>, key: string): number {
    const next = (counters.get(key) ?? 0) + 1;
    counters.set(key, next);
    return next;
  }

  #notifyAll(): void {
    const waiters = this.#waiters;
    this.#waiters = [];
    for (const wake of waiters) {
      wake();
    }
  }

  #changed(): Promise<void> {
    return new Promise((resolve) => {
      this.#waiters.push(resolve);
    });
  }

  async commitCall(record: CallRecord, plans: readonly TablePlan[]): Promise<boolean> {
    return (await this.commit(record, plans)).retained;
  }

  async commit(record: CallRecord, plans: readonly TablePlan[]): Promise<CommitOutcome> {
    const directTicket = record.retentionSeq <= 0;
    const ticketed = directTicket ? { ...record, retentionSeq: this.nextRetentionSeq() } : record;
    const sequence = ticketed.retentionSeq;
    let ready = false;
    let retry = false;
    let outcome: CommitOutcome;
    try {
      while (sequence !== this.#nextCommitSeq && sequence !== this.#retrySeq) {
        if (sequence < this.#nextCommitSeq) {
          throw new Error(`retention ticket ${sequence} is no longer active`);
        }
        await this.#changed();
      }
      retry = sequence === this.#retrySeq;
      ready = true;
      outcome = await this.#writeLock.run(() => this.write(ticketed, plans));
    } catch (error) {
      if (!ready) {
        this.abandonCommit(sequence);
      } else if (directTicket || retry) {
        this.#finishCommit(sequence);
      } else {
        this.#retrySeq = sequence;
        this.#notifyAll();
      }
      throw error;
    }
    this.#finishCommit(sequence);
    return outcome;
  }

  #finishCommit(sequence: number): void {
    if (this.#retrySeq === sequence) {
      this.#retrySeq = null;
    }
    this.#nextCommitSeq = Math.max(this.#nextCommitSeq, sequence + 1);
    this.#drainAbandoned();
    this.#notifyAll();
  }

  abandonCommit(sequence: number): void {
    if (sequence < this.#nextCommitSeq) {
      return;
    }
    this.#abandoned.add(sequence);
    if (this.#retrySeq === sequence) {
      this.#retrySeq = null;
    }
    this.#drainAbandoned();
    this.#notifyAll();
  }

  #drainAbandoned(): void {
    while (this.#abandoned.has(this.#nextCommitSeq)) {
      this.#abandoned.delete(this.#nextCommitSeq);
      this.#nextCommitSeq += 1;
    }
  }
}

export class Store extends CommitSequencer {
  readonly #instance: DuckDBInstance;
  readonly #connection: DuckDBConnection;
  readonly #maxSessionBytes: number;
  readonly #maxSessionCalls: number;
  readonly #generation: number;
  #retained = new Map<string, RetainedCall>();
  #metadata = new Map<string, CallMetadata>();
  #retainedBytes = 0;
  readonly #evictedTables = new Set<string>();
  readonly #evictedTableLimit: number;
  readonly #allowedObjects = new Set<string>();
  #closed = false;

  private constructor(instance: DuckDBInstance, connection: DuckDBConnection, limits: Limits, generation: number) {
    super();
    this.#instance = instance;
    this.#connection = connection;
    this.#maxSessionBytes = limits.max_session_bytes;
    this.#maxSessionCalls = limits.max_session_calls;
    this.#evictedTableLimit = limits.max_session_calls * 64;
    this.#generation = generation;
  }

  static async open(limits: Limits, generation = 0): Promise<Store> {
    const instance = await DuckDBInstance.create(":memory:");
    let connection: DuckDBConnection | undefined;
    try {
      connection = await instance.connect();
      for (const statement of lockdownStatements(limits.duckdb_max_memory)) {
        await connection.run(statement);
      }
      await verifyParserContract(connection);
      await connection.run(ENVELOPE_DDL);
      return new Store(instance, connection, limits, generation);
    } catch (error) {
      connection?.closeSync();
      instance.closeSync();
      throw error;
    }
  }

  get instance(): DuckDBInstance {
    return this.#instance;
  }

  get connection(): DuckDBConnection {
    return this.#connection;
  }

  get allowedObjects(): Set<string> {
    return new Set(this.#allowedObjects);
  }

  get retainedBytes(): number {
    return this.#retainedBytes;
  }

  get retainedCallIds(): Set<string> {
    return new Set(this.#retained.keys());
  }

  get evictedTables(): Set<string> {
    return new Set(this.#evictedTables);
  }

  get closed(): boolean {
    return this.#closed;
  }

  connect(): Promise<DuckDBConnection> {
    return this.#instance.connect();
  }

  close(): void {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.#connection.closeSync();
    this.#instance.closeSync();
  }

  protected write(record: CallRecord, plans: readonly TablePlan[]): Promise<CommitOutcome> {
    return this.commitNow(record, plans);
  }

  async runQuery(sql: string, limit: number, limits: Limits, deadline: number): Promise<QueryRows> {
    const control = new AbortController();
    const cancelTimer = longTimeout(() => control.abort(), Math.max(0, deadline - performance.now()));
    try {
      return await executeOnConnection(
        this,
        sql,
        limit,
        limits.max_cell_bytes,
        limits.query_max_bytes,
        control.signal,
        INTERRUPT_GRACE_MS,
      );
    } catch (error) {
      if (error instanceof QueryInterrupted) {
        throw timeoutFailure(limits.query_timeout_seconds);
      }
      throw error;
    } finally {
      cancelTimer();
    }
  }

  async commitNow(original: CallRecord, plans: readonly TablePlan[]): Promise<CommitOutcome> {
    const record: CallRecord = { ...original, flatReason: boundedFlatReason(original.flatReason) };
    const size = this.retentionSize(record, plans);
    const order = record.retentionSeq || this.retentionSequence + 1;
    const fitsBudget = size <= this.#maxSessionBytes;
    const storedRecord: CallRecord = fitsBudget
      ? record
      : {
          ...record,
          args: null,
          flatTables: [],
          sourcePaths: [],
          flatReason: retentionBudgetReason(record.flatReason),
          payload: { ...record.payload, value: undefined, text: null, blocks: [], structured: null },
        };
    const newEntry: RetainedCall = { order, size, tables: fitsBudget ? plans.map((plan) => plan.name) : [] };
    const retained = new Map(this.#retained);
    if (fitsBudget) {
      retained.set(record.callId, newEntry);
    }
    const metadata = new Map(this.#metadata);
    metadata.set(record.callId, { order, scopeId: record.scopeId, tables: newEntry.tables });
    const oldest = <T extends { readonly order: number }>(entries: Map<string, T>): string => {
      let best: string | undefined;
      let bestOrder = Number.POSITIVE_INFINITY;
      for (const [callId, entry] of entries) {
        if (entry.order < bestOrder || (entry.order === bestOrder && best !== undefined && callId < best)) {
          best = callId;
          bestOrder = entry.order;
        }
      }
      if (best === undefined) {
        throw new Error("no call to evict");
      }
      return best;
    };
    const connection = this.#connection;
    await connection.run("BEGIN TRANSACTION");
    let view: string;
    try {
      for (const plan of fitsBudget ? plans : []) {
        await this.#createFlat(plan, record.callId);
      }
      await this.#insertEnvelope(storedRecord);
      view = await this.#ensureScopeView(storedRecord.scopeId);
      while (Store.#total(retained) > this.#maxSessionBytes) {
        const victimId = oldest(retained);
        const victim = retained.get(victimId);
        retained.delete(victimId);
        if (victim !== undefined) {
          await this.#evict(victimId, victim, "retention_evicted");
        }
      }
      while (metadata.size > this.#maxSessionCalls) {
        const victimId = oldest(metadata);
        const victim = metadata.get(victimId);
        metadata.delete(victimId);
        retained.delete(victimId);
        if (victim !== undefined) {
          await this.#deleteCall(victimId, victim);
        }
      }
      await connection.run("COMMIT");
    } catch (error) {
      await connection.run("ROLLBACK").catch(() => undefined);
      throw error;
    }
    const newlyEvicted: string[] = [];
    const allEntries = new Map(this.#retained);
    allEntries.set(record.callId, newEntry);
    for (const [callId, entry] of allEntries) {
      if (retained.has(callId)) {
        continue;
      }
      for (const table of entry.tables) {
        this.#allowedObjects.delete(table);
      }
      this.#rememberEvicted(entry.tables);
      newlyEvicted.push(...entry.tables);
      const meta = metadata.get(callId);
      if (meta !== undefined) {
        metadata.set(callId, { ...meta, tables: [] });
      }
    }
    this.#retained = retained;
    this.#retainedBytes = Store.#total(retained);
    const removed = [...this.#metadata.keys()].filter((callId) => !metadata.has(callId));
    for (const callId of removed) {
      const entry = this.#metadata.get(callId);
      if (entry !== undefined) {
        for (const table of entry.tables) {
          this.#allowedObjects.delete(table);
        }
        this.#rememberEvicted(entry.tables);
        newlyEvicted.push(...entry.tables);
      }
    }
    const remainingScopes = new Set([...metadata.values()].map((entry) => entry.scopeId));
    for (const callId of removed) {
      const entry = this.#metadata.get(callId);
      if (entry !== undefined && !remainingScopes.has(entry.scopeId)) {
        this.#allowedObjects.delete(scopeViewName(entry.scopeId, this.#generation));
      }
    }
    this.#metadata = metadata;
    for (const entry of retained.values()) {
      for (const table of entry.tables) {
        this.#allowedObjects.add(table);
      }
    }
    this.#allowedObjects.add(view);
    return { retained: fitsBudget, envelope: view, evicted: newlyEvicted };
  }

  static #total(retained: ReadonlyMap<string, RetainedCall>): number {
    let total = 0;
    for (const entry of retained.values()) {
      total += entry.size;
    }
    return total;
  }

  retentionSize(record: CallRecord, plans: readonly TablePlan[]): number {
    const payload = record.payload;
    const jsonFields: (JsonValue | null | undefined)[] = [
      record.args,
      payload.value,
      storedBlocks(payload),
      payload.structured,
      strings(record.contentKinds),
      strings(record.flatTables),
      strings(record.sourcePaths),
    ];
    let size = 0;
    for (const value of jsonFields) {
      if (value !== null && value !== undefined) {
        size += jsonSize(value);
      }
    }
    const textFields = [
      record.callId,
      record.scopeId,
      record.server,
      record.tool,
      payload.text,
      payload.channel,
      record.failureClass,
      record.flatReason,
    ];
    for (const value of textFields) {
      if (value !== null) {
        size += byteLength(value);
      }
    }
    const scalarFields = [
      String(record.seq),
      payload.conflict ? "True" : "False",
      String(payload.byteSize),
      payload.wireBytes === null ? null : String(payload.wireBytes),
      record.isError ? "True" : "False",
      pythonDatetime(record.startedAt),
      pythonDatetime(record.endedAt),
      String(durationMs(record)),
    ];
    for (const value of scalarFields) {
      if (value !== null) {
        size += byteLength(value);
      }
    }
    for (const plan of plans) {
      size += byteLength(plan.name) + byteLength(plan.sourcePath);
      for (const row of plan.records) {
        size += jsonSize(row);
      }
    }
    return Math.max(size, 1);
  }

  async #evict(callId: string, entry: RetainedCall, reason: string): Promise<void> {
    for (const table of entry.tables) {
      await this.#connection.run(`DROP TABLE IF EXISTS ${quoteIdent(table)}`);
    }
    await this.#connection.run(
      `UPDATE ${quoteIdent(ENVELOPE_TABLE)} SET args = NULL, result = NULL, result_text = NULL, ` +
        "result_blocks = NULL, result_structured = NULL, flat_tables = [], source_paths = [], " +
        "flat_reason = CASE WHEN flat_reason IS NULL THEN $1 ELSE $1 || '; prior=' || flat_reason END " +
        "WHERE call_id = $2",
      [reason, callId],
      [VARCHAR, VARCHAR],
    );
  }

  #rememberEvicted(tables: readonly string[]): void {
    for (const table of tables) {
      this.#evictedTables.delete(table);
      this.#evictedTables.add(table);
      while (this.#evictedTables.size > this.#evictedTableLimit) {
        const first = this.#evictedTables.values().next();
        if (first.done === true) {
          break;
        }
        this.#evictedTables.delete(first.value);
      }
    }
  }

  async #deleteCall(callId: string, entry: CallMetadata): Promise<void> {
    for (const table of entry.tables) {
      await this.#connection.run(`DROP TABLE IF EXISTS ${quoteIdent(table)}`);
    }
    await this.#connection.run(`DELETE FROM ${quoteIdent(ENVELOPE_TABLE)} WHERE call_id = $1`, [callId], [VARCHAR]);
    const remaining = await this.#connection.runAndReadAll(
      `SELECT 1 FROM ${quoteIdent(ENVELOPE_TABLE)} WHERE scope_id = $1 LIMIT 1`,
      [entry.scopeId],
      [VARCHAR],
    );
    if (remaining.currentRowCount === 0) {
      await this.#connection.run(`DROP VIEW IF EXISTS ${quoteIdent(scopeViewName(entry.scopeId, this.#generation))}`);
    }
  }

  async #ensureScopeView(scopeId: string): Promise<string> {
    if (!SCOPE_PATTERN.test(scopeId)) {
      throw new RangeError(`scope id is not 32 hex characters: ${JSON.stringify(scopeId)}`);
    }
    const name = scopeViewName(scopeId, this.#generation);
    await this.#connection.run(
      `CREATE OR REPLACE VIEW ${quoteIdent(name)} AS SELECT * FROM ${quoteIdent(ENVELOPE_TABLE)} WHERE scope_id = '${scopeId}'`,
    );
    return name;
  }

  async #insertEnvelope(record: CallRecord): Promise<void> {
    const payload = record.payload;
    const json = (value: JsonValue | null | undefined): string | null =>
      value === null || value === undefined ? null : wellFormedJson(dumpJson(value));
    const text = (value: string | null): string | null => (value === null ? null : value.toWellFormed());
    const texts = (values: readonly string[]): DuckDBValue => listValue(values.map((value) => value.toWellFormed()));
    const values: DuckDBValue[] = [
      record.callId,
      record.scopeId,
      BigInt(record.seq),
      text(record.server),
      text(record.tool),
      json(record.args),
      json(payload.value),
      text(payload.text),
      json(storedBlocks(payload)),
      json(payload.structured),
      payload.channel,
      payload.conflict,
      BigInt(payload.byteSize),
      optionalBigInt(payload.wireBytes),
      record.isError,
      text(record.failureClass),
      texts(record.contentKinds),
      texts(record.flatTables),
      text(record.flatReason),
      texts(record.sourcePaths),
      pythonDatetime(record.startedAt),
      pythonDatetime(record.endedAt),
      BigInt(durationMs(record)),
    ];
    await this.#connection.run(ENVELOPE_INSERT, values, [...ENVELOPE_TYPES]);
  }

  async #createFlat(plan: TablePlan, callId: string): Promise<void> {
    const declared = plan.columns.map((column) => `${quoteIdent(column.name)} ${column.type}`);
    declared.push(`${quoteIdent(ROW_COLUMN)} BIGINT`, `${quoteIdent(CALL_COLUMN)} VARCHAR`);
    await this.#connection.run(`CREATE TABLE ${quoteIdent(plan.name)} (${declared.join(", ")})`);
    if (plan.records.length === 0) {
      return;
    }
    const appender = await this.#connection.createAppender(plan.name);
    try {
      for (const record of plan.records) {
        for (const column of plan.columns) {
          appendCell(appender, record.get(column.name) ?? null, column);
        }
        const row = record.get(ROW_COLUMN);
        if (typeof row !== "bigint") {
          throw new TypeError(`row ${String(row)} has no ${ROW_COLUMN}`);
        }
        appender.appendBigInt(row);
        appender.appendVarchar(callId);
        appender.endRow();
      }
      appender.flushSync();
    } catch (error) {
      discard(appender);
      throw error;
    }
    appender.closeSync();
  }
}
