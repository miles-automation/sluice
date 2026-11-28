import type { DuckDBConnection, DuckDBPreparedStatement, DuckDBResult } from "@duckdb/node-api";
import { QueryRejectedError, check, prepareSelect } from "./gate.ts";
import { truncateToBytes } from "./payload.ts";

export const INTERRUPT_RETRY_MS = 50;
export const CELL_SLACK_BYTES = 4;

export type Cell = string | null;

export interface QueryRows {
  readonly columns: readonly string[];
  readonly rows: readonly (readonly Cell[])[];
}

export interface QuerySource {
  connect(): Promise<DuckDBConnection>;
  readonly allowedObjects: Set<string>;
  readonly evictedTables: Set<string>;
}

export class QueryInterrupted extends Error {
  override readonly name = "QueryInterrupted";
}

export class AllocationFailed extends QueryRejectedError {}

const SUGGESTION = /\n?Did you mean [^\n]*/gu;
const MISSING_TABLE = /^Catalog Error: Table with name /u;
const BAD_ALLOCATION = /bad[ _]alloc/iu;

export const VANISHED_TABLE = "a table this query names is no longer available; it may have been evicted";

export function withoutSuggestions(text: string): string {
  return text.replace(SUGGESTION, "");
}

export function isAllocationFailure(error: unknown): boolean {
  if (error instanceof RangeError && /allocation failed|out of memory/iu.test(error.message)) {
    return true;
  }
  return error instanceof Error && /^Out of Memory Error:/u.test(error.message) && BAD_ALLOCATION.test(error.message);
}

function errorName(error: unknown): string {
  if (!(error instanceof Error)) {
    return "Error";
  }
  const match = /^([A-Za-z ]+) Error:/u.exec(error.message);
  return match?.[1] === undefined ? error.name : `${match[1].replaceAll(" ", "")}Error`;
}

export function engineFailure(error: unknown): QueryRejectedError {
  const text = error instanceof Error ? error.message : String(error);
  if (MISSING_TABLE.test(text)) {
    return new QueryRejectedError(VANISHED_TABLE);
  }
  const message = `${errorName(error)}: ${withoutSuggestions(text)}`;
  return isAllocationFailure(error) ? new AllocationFailed(message) : new QueryRejectedError(message);
}

export function timeoutFailure(seconds: number): QueryRejectedError {
  return new QueryRejectedError(`query exceeded the ${seconds}s timeout and was interrupted`);
}

export function cellText(value: unknown, maxBytes: number): Cell {
  if (value === null || value === undefined) {
    return null;
  }
  const [text] = truncateToBytes(String(value), maxBytes);
  return text;
}

function isInterrupt(error: unknown): boolean {
  return error instanceof Error && /^INTERRUPT Error:/u.test(error.message);
}

export async function executeOnConnection(
  source: QuerySource,
  sql: string,
  limit: number,
  cellBytes: number,
  signal: AbortSignal,
): Promise<QueryRows> {
  let connection: DuckDBConnection | undefined;
  let retry: ReturnType<typeof setInterval> | undefined;
  let finished = false;
  let expire = (): void => undefined;
  const expired = (): boolean => signal.aborted;
  const deadline = new Promise<never>((_, reject) => {
    expire = (): void => {
      reject(new QueryInterrupted());
      if (finished) {
        return;
      }
      connection?.interrupt();
      retry = setInterval(() => connection?.interrupt(), INTERRUPT_RETRY_MS);
    };
  });
  if (signal.aborted) {
    expire();
  } else {
    signal.addEventListener("abort", expire, { once: true });
  }
  const guard = (): void => {
    if (expired()) {
      throw new QueryInterrupted();
    }
  };
  const work = (async (): Promise<QueryRows> => {
    let prepared: DuckDBPreparedStatement | undefined;
    let result: DuckDBResult | undefined;
    let drained = false;
    try {
      guard();
      connection = await source.connect();
      guard();
      await check(sql, connection, source.allowedObjects, source.evictedTables);
      guard();
      prepared = await prepareSelect(sql, connection);
      guard();
      result = await prepared.stream();
      const cellCap = cellBytes + CELL_SLACK_BYTES;
      const columns = result.columnNames();
      const rows: Cell[][] = [];
      while (rows.length <= limit) {
        guard();
        const chunk = await result.fetchChunk();
        if (chunk === null || chunk.rowCount === 0) {
          drained = true;
          break;
        }
        const vectors = Array.from({ length: chunk.columnCount }, (_, column) => chunk.getColumnVector(column));
        for (let index = 0; index < chunk.rowCount && rows.length <= limit; index += 1) {
          rows.push(vectors.map((vector) => cellText(vector.getItem(index), cellCap)));
        }
      }
      guard();
      return { columns, rows };
    } finally {
      finished = true;
      clearInterval(retry);
      if (!drained) {
        connection?.interrupt();
      }
      result?.closeSync();
      prepared?.destroySync();
      connection?.closeSync();
    }
  })();
  work.catch(() => undefined);
  deadline.catch(() => undefined);
  try {
    return await Promise.race([work, deadline]);
  } catch (error) {
    if (error instanceof QueryInterrupted || expired() || isInterrupt(error)) {
      throw new QueryInterrupted();
    }
    if (error instanceof QueryRejectedError) {
      throw new QueryRejectedError(withoutSuggestions(error.message));
    }
    throw engineFailure(error);
  } finally {
    signal.removeEventListener("abort", expire);
  }
}
