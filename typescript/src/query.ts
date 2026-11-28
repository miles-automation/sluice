import { QueryRejectedError } from "./gate.ts";
import { QUERY_OUTPUT_MIN_BYTES, type Limits } from "./limits.ts";
import { byteLength, truncateToBytes } from "./payload.ts";
import type { CommitSequencer } from "./store.ts";
import { Semaphore } from "./sync.ts";
import { queryConcurrency } from "./threadpool.ts";

export {
  CELL_SLACK_BYTES,
  INTERRUPT_RETRY_MS,
  VANISHED_TABLE,
  cellText,
  engineFailure,
  withoutSuggestions,
} from "./execute.ts";

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

export class QueryTool {
  readonly #store: CommitSequencer;
  readonly #limits: Limits;
  readonly #admission: Semaphore;
  readonly #capacity: number;

  constructor(store: CommitSequencer, limits: Limits, options: QueryToolOptions = {}) {
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
    const deadline = performance.now() + this.#limits.query_timeout_seconds * 1000;
    const execution = await this.#admission.run(() => this.#store.runQuery(sql, limit, this.#limits, deadline));
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
