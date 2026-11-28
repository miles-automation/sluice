import type { DuckDBConnection, DuckDBPreparedStatement, DuckDBResult } from "@duckdb/node-api";
import { QueryRejectedError, check, prepareSelect } from "./gate.ts";
import { QUERY_OUTPUT_MIN_BYTES } from "./limits.ts";
import { byteLength, truncateToBytes } from "./payload.ts";

export const INTERRUPT_RETRY_MS = 50;
export const INTERRUPT_GRACE_MS = 5000;
export const CELL_SLACK_BYTES = 4;

export type Cell = string | null;

export interface QueryRows {
  readonly columns: readonly string[];
  readonly rows: readonly (readonly Cell[])[];
  readonly moreRows: boolean;
  readonly omitted: number;
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

export function escapeCell(value: unknown, maxBytes: number): readonly [string, boolean] {
  if (value === null || value === undefined) {
    return ["NULL", false];
  }
  let text = String(value);
  if (text === "") {
    return ["''", false];
  }
  text = text.replaceAll("\\", "\\\\").replaceAll("|", "\\|");
  text = text.replaceAll("\r\n", "\\n").replaceAll("\n", "\\n").replaceAll("\r", "\\n");
  if (byteLength(text) <= maxBytes) {
    return [text, false];
  }
  const marker = "…";
  const markerBytes = byteLength(marker);
  const [rendered] = truncateToBytes(text, Math.max(maxBytes - markerBytes, 0));
  return [rendered + (maxBytes >= markerBytes ? marker : ""), true];
}

export function boundRows(
  columns: readonly string[],
  fetched: readonly (readonly Cell[])[],
  limit: number,
  queryMaxBytes: number,
  maxCellBytes: number,
): QueryRows {
  const moreRows = fetched.length > limit;
  const rows = fetched.slice(0, limit);
  if (columns.length === 0) {
    return { columns, rows, moreRows, omitted: 0 };
  }
  const budget = Math.max(queryMaxBytes - QUERY_OUTPUT_MIN_BYTES - 2, 0);
  let used = 3;
  for (const [index, name] of columns.entries()) {
    used += byteLength(escapeCell(name, maxCellBytes)[0]) + 9;
    if (used > budget) {
      return { columns: columns.slice(0, index + 1), rows: [], moreRows, omitted: rows.length };
    }
  }
  for (const [index, row] of rows.entries()) {
    let line = 4 + 3 * Math.max(row.length - 1, 0);
    for (const value of row) {
      line += byteLength(escapeCell(value, maxCellBytes)[0]);
    }
    if (used + line + 1 > budget) {
      return { columns, rows: rows.slice(0, index), moreRows, omitted: rows.length - index };
    }
    used += line + 1;
  }
  return { columns, rows, moreRows, omitted: 0 };
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
  outputBytes: number,
  signal: AbortSignal,
  abandonAfterMs: number | null = null,
): Promise<QueryRows> {
  let connection: DuckDBConnection | undefined;
  let retry: ReturnType<typeof setInterval> | undefined;
  let finished = false;
  const expired = (): boolean => signal.aborted;
  let abandon: ReturnType<typeof setTimeout> | undefined;
  let giveUp = (): void => undefined;
  const abandoned = new Promise<never>((_, reject) => {
    giveUp = () => reject(new QueryInterrupted());
  });
  abandoned.catch(() => undefined);
  const expire = (): void => {
    if (finished) {
      return;
    }
    connection?.interrupt();
    retry = setInterval(() => connection?.interrupt(), INTERRUPT_RETRY_MS);
    if (abandonAfterMs !== null) {
      abandon = setTimeout(giveUp, abandonAfterMs);
    }
  };
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
      const columns = result.columnNames().map((name) => cellText(name, cellCap) ?? "");
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
      return boundRows(columns, rows, limit, outputBytes, cellBytes);
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
  try {
    const rows = await Promise.race([work, abandoned]);
    if (expired()) {
      throw new QueryInterrupted();
    }
    return rows;
  } catch (error) {
    if (error instanceof QueryInterrupted || expired() || isInterrupt(error)) {
      throw new QueryInterrupted();
    }
    if (error instanceof QueryRejectedError) {
      throw new QueryRejectedError(withoutSuggestions(error.message));
    }
    throw engineFailure(error);
  } finally {
    clearTimeout(abandon);
    clearInterval(retry);
    signal.removeEventListener("abort", expire);
  }
}
