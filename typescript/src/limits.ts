export const SESSION_RETENTION_DEFAULT = 256 * 1024 * 1024;
export const SESSION_CALLS_DEFAULT = 1000;
export const QUERY_OUTPUT_MIN_BYTES = 384;
export const CELL_OUTPUT_MIN_BYTES = 4;

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
  return Object.freeze(limits);
}
