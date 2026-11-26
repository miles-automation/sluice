import type { DuckDBConnection, DuckDBPreparedStatement, DuckDBResult } from "@duckdb/node-api";
import { QueryRejectedError, check, prepareSelect } from "./gate.ts";
import { QUERY_OUTPUT_MIN_BYTES, type Limits } from "./limits.ts";
import { byteLength, truncateToBytes } from "./payload.ts";
import type { Store } from "./store.ts";
import { Semaphore } from "./sync.ts";
import { queryConcurrency } from "./threadpool.ts";

export const QUERY_TOOL_NAME = "query";

export const QUERY_DESCRIPTION =
  "Run one read-only SQL SELECT over the tables materialized from earlier tool " +
  "results in this conversation. You can only reference tables named in the " +
  "handles you were given; there is no way to list what exists. Column names " +
  "come verbatim from the source payload, so quote any that are not plain " +
  "identifiers. Columns marked JSON need json_extract and a cast before " +
  "arithmetic, and columns marked inexact fall outside the range where results " +
  "are guaranteed to match the source exactly. Older tables may be evicted by " +
  "the session retention budget; rerun the source tool call if that happens.";

export const QUERY_SCHEMA = {
  type: "object",
  properties: {
    sql: { type: "string", description: "A single SELECT statement." },
    max_rows: { type: "integer", description: "Maximum rows to return.", minimum: 1 },
  },
  required: ["sql"],
  additionalProperties: false,
} as const;

export const NOTES_RESERVE = QUERY_OUTPUT_MIN_BYTES;
export const INTERRUPT_RETRY_MS = 50;
export const CELL_SLACK_BYTES = 4;

export interface QueryOutcome {
  readonly columns: readonly string[];
  readonly rows: readonly (readonly (string | null)[])[];
  readonly moreRows: boolean;
  readonly truncatedCells?: number;
  readonly truncatedHeaders?: number;
  readonly droppedRows?: number;
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

export function cellText(value: unknown, maxBytes: number): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  const [text] = truncateToBytes(String(value), maxBytes);
  return text;
}

export function render(outcome: QueryOutcome, limits: Limits): string {
  const lines: string[] = [];
  let shown = 0;
  let droppedRows = outcome.droppedRows ?? 0;
  let truncatedCells = outcome.truncatedCells ?? 0;
  let truncatedHeaders = outcome.truncatedHeaders ?? 0;
  let tableOmitted = false;
  const bodyBudget = Math.max(limits.query_max_bytes - NOTES_RESERVE - 2, 0);
  if (outcome.columns.length === 0) {
    if (byteLength("(no columns)") <= bodyBudget) {
      lines.push("(no columns)");
    } else {
      tableOmitted = true;
    }
  } else {
    const headerParts = outcome.columns.map((name) => escapeCell(name, limits.max_cell_bytes));
    const header = `| ${headerParts.map(([text]) => text).join(" | ")} |`;
    const divider = `| ${headerParts.map(() => "---").join(" | ")} |`;
    let used = byteLength(header) + byteLength(divider) + 1;
    if (used > bodyBudget) {
      tableOmitted = true;
      droppedRows += outcome.rows.length;
    } else {
      lines.push(header, divider);
      truncatedHeaders += headerParts.filter(([, cut]) => cut).length;
      for (const [index, row] of outcome.rows.entries()) {
        const cells = row.map((value) => escapeCell(value, limits.max_cell_bytes));
        const line = `| ${cells.map(([text]) => text).join(" | ")} |`;
        if (used + byteLength(line) + 1 > bodyBudget) {
          droppedRows += outcome.rows.length - index;
          break;
        }
        used += byteLength(line) + 1;
        truncatedCells += cells.filter(([, cut]) => cut).length;
        lines.push(line);
        shown += 1;
      }
    }
  }
  const notes = [`${shown} row(s) shown.`];
  if (outcome.moreRows) {
    notes.push("Additional rows exist beyond max_rows; the exact number is not known.");
  }
  if (droppedRows > 0) {
    notes.push(`${droppedRows} row(s) omitted to stay under the byte cap.`);
  }
  if (truncatedCells > 0) {
    notes.push(`${truncatedCells} cell(s) truncated, marked with a trailing ….`);
  }
  if (truncatedHeaders > 0) {
    notes.push(`${truncatedHeaders} column header(s) truncated, marked with a trailing ….`);
  }
  if (tableOmitted) {
    notes.push("The table was omitted because its header exceeded the output byte cap.");
  }
  const notice = notes.join(" ");
  const body = lines.join("\n");
  const text = body ? `${body}\n\n${notice}` : notice;
  if (byteLength(text) <= limits.query_max_bytes) {
    return text;
  }
  const fallback = "Output truncated to stay under the configured byte cap.";
  const [prefix] = truncateToBytes(body, Math.max(limits.query_max_bytes - byteLength(fallback) - 2, 0));
  return prefix ? `${prefix}\n\n${fallback}` : fallback;
}

export interface QueryToolOptions {
  readonly concurrency?: number;
}

class DeadlineExpired extends Error {
  override readonly name = "DeadlineExpired";
}

function isInterrupt(error: unknown): boolean {
  return error instanceof Error && /^INTERRUPT Error:/u.test(error.message);
}

function errorName(error: unknown): string {
  if (!(error instanceof Error)) {
    return "Error";
  }
  const match = /^([A-Za-z ]+) Error:/u.exec(error.message);
  return match?.[1] === undefined ? error.name : `${match[1].replaceAll(" ", "")}Error`;
}

interface Execution {
  readonly columns: string[];
  readonly rows: (string | null)[][];
}

export class QueryTool {
  readonly #store: Store;
  readonly #limits: Limits;
  readonly #admission: Semaphore;
  readonly #capacity: number;

  constructor(store: Store, limits: Limits, options: QueryToolOptions = {}) {
    this.#store = store;
    this.#limits = limits;
    this.#capacity = options.concurrency ?? queryConcurrency();
    this.#admission = new Semaphore(this.#capacity);
  }

  get capacity(): number {
    return this.#capacity;
  }

  get inFlight(): number {
    return this.#capacity - this.#admission.available;
  }

  async run(sql: string, maxRows?: number | null): Promise<string> {
    const limit = this.#resolveLimit(maxRows);
    const execution = await this.#execute(sql, limit);
    return render(
      { columns: execution.columns, rows: execution.rows.slice(0, limit), moreRows: execution.rows.length > limit },
      this.#limits,
    );
  }

  #resolveLimit(maxRows: number | null | undefined): number {
    if (maxRows === null || maxRows === undefined) {
      return this.#limits.query_max_rows;
    }
    if (maxRows < 1) {
      throw new QueryRejectedError(`max_rows must be at least 1, got ${maxRows}`);
    }
    return Math.min(maxRows, this.#limits.query_max_rows);
  }

  #timeoutFailure(): QueryRejectedError {
    return new QueryRejectedError(
      `query exceeded the ${this.#limits.query_timeout_seconds}s timeout and was interrupted`,
    );
  }

  async #execute(sql: string, limit: number): Promise<Execution> {
    let expired = false;
    let connection: DuckDBConnection | undefined;
    let retry: ReturnType<typeof setInterval> | undefined;
    let expire!: () => void;
    const deadline = new Promise<never>((_, reject) => {
      expire = () => {
        expired = true;
        reject(new DeadlineExpired());
        connection?.interrupt();
        retry = setInterval(() => connection?.interrupt(), INTERRUPT_RETRY_MS);
      };
    });
    const timer = setTimeout(expire, this.#limits.query_timeout_seconds * 1000);
    const guard = (): void => {
      if (expired) {
        throw new DeadlineExpired();
      }
    };
    const work = (async (): Promise<Execution> => {
      const release = await this.#admission.acquire();
      let prepared: DuckDBPreparedStatement | undefined;
      let result: DuckDBResult | undefined;
      let drained = false;
      try {
        guard();
        connection = await this.#store.connect();
        guard();
        await check(sql, connection, this.#store.allowedObjects, this.#store.evictedTables);
        guard();
        prepared = await prepareSelect(sql, connection);
        guard();
        result = await prepared.stream();
        const cellCap = this.#limits.max_cell_bytes + CELL_SLACK_BYTES;
        const columns = result.columnNames();
        const rows: (string | null)[][] = [];
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
        clearInterval(retry);
        if (!drained) {
          connection?.interrupt();
        }
        result?.closeSync();
        prepared?.destroySync();
        connection?.closeSync();
        release();
      }
    })();
    work.catch(() => undefined);
    deadline.catch(() => undefined);
    try {
      return await Promise.race([work, deadline]);
    } catch (error) {
      if (error instanceof DeadlineExpired || expired || isInterrupt(error)) {
        throw this.#timeoutFailure();
      }
      if (error instanceof QueryRejectedError) {
        throw new QueryRejectedError(withoutSuggestions(error.message));
      }
      throw engineFailure(error);
    } finally {
      clearTimeout(timer);
    }
  }
}

const SUGGESTION = /\n?Did you mean [^\n]*/gu;
const MISSING_TABLE = /^Catalog Error: Table with name /u;

export const VANISHED_TABLE = "a table this query names is no longer available; it may have been evicted";

export function withoutSuggestions(text: string): string {
  return text.replace(SUGGESTION, "");
}

export function engineFailure(error: unknown): QueryRejectedError {
  const text = error instanceof Error ? error.message : String(error);
  if (MISSING_TABLE.test(text)) {
    return new QueryRejectedError(VANISHED_TABLE);
  }
  return new QueryRejectedError(`${errorName(error)}: ${withoutSuggestions(text)}`);
}

export interface QueryToolResult {
  readonly content: readonly { readonly type: "text"; readonly text: string }[];
  readonly isError?: true;
}

function textError(text: string): QueryToolResult {
  return { content: [{ type: "text", text }], isError: true };
}

export async function runQueryTool(query: QueryTool, args: unknown): Promise<QueryToolResult> {
  const record = typeof args === "object" && args !== null && !Array.isArray(args) ? (args as Record<string, unknown>) : {};
  const sql = record["sql"];
  if (typeof sql !== "string" || sql.trim() === "") {
    return textError("query needs a `sql` string");
  }
  const maxRows = record["max_rows"];
  if (maxRows !== undefined && maxRows !== null && !(typeof maxRows === "number" && Number.isSafeInteger(maxRows))) {
    return textError("max_rows must be an integer");
  }
  try {
    return { content: [{ type: "text", text: await query.run(sql, maxRows) }] };
  } catch (error) {
    if (error instanceof QueryRejectedError) {
      return textError(error.message);
    }
    throw error;
  }
}
