export const SESSION_RETENTION_DEFAULT = 256 * 1024 * 1024;
export const SESSION_CALLS_DEFAULT = 1000;
export const QUERY_OUTPUT_MIN_BYTES = 384;
export const CELL_OUTPUT_MIN_BYTES = 4;
export const STORE_MEMORY_DEFAULT = 2 * 1024 * 1024 * 1024;
export const STORE_MEMORY_MIN = 128 * 1024 * 1024;

export class LimitsError extends Error {
  override readonly name = "LimitsError";
}

export interface Limits {
  readonly max_payload_bytes: number;
  readonly max_concurrent_materializations: number;
  readonly preview_bytes: number;
  readonly preview_rows: number;
  readonly max_columns: number;
  readonly query_timeout_seconds: number;
  readonly query_max_rows: number;
  readonly query_max_bytes: number;
  readonly max_cell_bytes: number;
  readonly duckdb_max_memory: string;
  readonly max_session_bytes: number;
  readonly max_session_calls: number;
  readonly store_max_memory: number;
}

export const DEFAULT_LIMITS: Limits = {
  max_payload_bytes: 1_048_576,
  max_concurrent_materializations: 2,
  preview_bytes: 2048,
  preview_rows: 3,
  max_columns: 64,
  query_timeout_seconds: 10,
  query_max_rows: 100,
  query_max_bytes: 65_536,
  max_cell_bytes: 512,
  duckdb_max_memory: "1GB",
  max_session_bytes: SESSION_RETENTION_DEFAULT,
  max_session_calls: SESSION_CALLS_DEFAULT,
  store_max_memory: STORE_MEMORY_DEFAULT,
};

const POSITIVE = [
  "max_payload_bytes",
  "max_concurrent_materializations",
  "max_columns",
  "query_max_rows",
  "query_max_bytes",
  "max_cell_bytes",
  "max_session_bytes",
  "max_session_calls",
  "store_max_memory",
] as const;

const NON_NEGATIVE = ["preview_bytes", "preview_rows"] as const;

function describe(value: unknown): string {
  return typeof value === "string" ? JSON.stringify(value) : String(value);
}

export function makeLimits(overrides: Partial<Record<keyof Limits, unknown>> = {}): Limits {
  const merged: Record<string, unknown> = { ...DEFAULT_LIMITS };
  for (const [key, value] of Object.entries(overrides)) {
    if (!Object.hasOwn(DEFAULT_LIMITS, key)) {
      throw new LimitsError(`[limits].${key} is not a known limit`);
    }
    if (value !== undefined) {
      merged[key] = value;
    }
  }
  for (const name of [...POSITIVE, ...NON_NEGATIVE]) {
    const value = merged[name];
    if (typeof value !== "number" || !Number.isSafeInteger(value)) {
      throw new LimitsError(`[limits].${name} must be an integer, got ${typeof value}: ${describe(value)}`);
    }
  }
  for (const name of POSITIVE) {
    const value = merged[name] as number;
    if (value < 1) {
      throw new LimitsError(`[limits].${name} must be at least 1, got ${value}`);
    }
  }
  for (const name of NON_NEGATIVE) {
    const value = merged[name] as number;
    if (value < 0) {
      throw new LimitsError(`[limits].${name} must not be negative, got ${value}`);
    }
  }
  const limits = merged as unknown as Limits;
  if (limits.max_session_bytes < limits.max_payload_bytes) {
    throw new LimitsError(
      `[limits].max_session_bytes must be at least max_payload_bytes (${limits.max_payload_bytes}), got ${limits.max_session_bytes}`,
    );
  }
  if (limits.query_max_bytes < QUERY_OUTPUT_MIN_BYTES) {
    throw new LimitsError(
      `[limits].query_max_bytes must be at least ${QUERY_OUTPUT_MIN_BYTES} so truncation notices cannot be truncated themselves`,
    );
  }
  if (limits.max_cell_bytes < CELL_OUTPUT_MIN_BYTES) {
    throw new LimitsError(
      `[limits].max_cell_bytes must be at least ${CELL_OUTPUT_MIN_BYTES} so a truncated cell can carry its ellipsis`,
    );
  }
  const timeout: unknown = limits.query_timeout_seconds;
  if (typeof timeout !== "number") {
    throw new LimitsError(`[limits].query_timeout_seconds must be a number, got ${typeof timeout}: ${describe(timeout)}`);
  }
  if (!Number.isFinite(timeout)) {
    throw new LimitsError(`[limits].query_timeout_seconds must be finite, got ${timeout}`);
  }
  if (timeout <= 0) {
    throw new LimitsError(`[limits].query_timeout_seconds must be positive, got ${timeout}`);
  }
  const memory: unknown = limits.duckdb_max_memory;
  if (typeof memory !== "string") {
    throw new LimitsError(`[limits].duckdb_max_memory must be a string, got ${typeof memory}`);
  }
  if (memory.trim() === "") {
    throw new LimitsError("[limits].duckdb_max_memory must not be empty");
  }
  const engineBytes = parseMemorySize(memory);
  if (engineBytes === null) {
    throw new LimitsError(
      `[limits].duckdb_max_memory must be a size such as '1GB' or '512MiB' so it can be compared with store_max_memory, got ${describe(memory)}`,
    );
  }
  if (limits.store_max_memory < STORE_MEMORY_MIN) {
    throw new LimitsError(
      `[limits].store_max_memory must be at least ${STORE_MEMORY_MIN} bytes (128 MiB), got ${limits.store_max_memory}`,
    );
  }
  if (BigInt(limits.store_max_memory) <= engineBytes) {
    throw new LimitsError(
      `[limits].store_max_memory (${limits.store_max_memory} bytes) must be greater than duckdb_max_memory (${describe(memory)} = ${engineBytes} bytes)`,
    );
  }
  return Object.freeze(limits);
}

const MEMORY_SIZE = /^\s*([0-9]+)(?:\.([0-9]+))?\s*([A-Za-z]+)\s*$/u;
const DECIMAL = [1n, 1000n, 1000n ** 2n, 1000n ** 3n, 1000n ** 4n] as const;
const MEMORY_UNITS: ReadonlyMap<string, bigint> = new Map([
  ["b", 1n],
  ["byte", 1n],
  ["bytes", 1n],
  ...(["k", "kb", "kilobyte", "kilobytes"] as const).map((unit) => [unit, DECIMAL[1]] as const),
  ...(["m", "mb", "megabyte", "megabytes"] as const).map((unit) => [unit, DECIMAL[2]] as const),
  ...(["g", "gb", "gigabyte", "gigabytes"] as const).map((unit) => [unit, DECIMAL[3]] as const),
  ...(["t", "tb", "terabyte", "terabytes"] as const).map((unit) => [unit, DECIMAL[4]] as const),
  ["kib", 1024n],
  ["mib", 1024n ** 2n],
  ["gib", 1024n ** 3n],
  ["tib", 1024n ** 4n],
]);

export function parseMemorySize(text: string): bigint | null {
  const match = MEMORY_SIZE.exec(text);
  if (match === null) {
    return null;
  }
  const multiplier = MEMORY_UNITS.get((match[3] ?? "").toLowerCase());
  if (multiplier === undefined) {
    return null;
  }
  const fraction = match[2] ?? "";
  const scale = 10n ** BigInt(fraction.length);
  return ((BigInt(match[1] ?? "0") * scale + BigInt(fraction || "0")) * multiplier) / scale;
}
