import { longTimeout } from "./timer.ts";
import type { PaginationConfig } from "./config.ts";
import { DownstreamError } from "./errors.ts";
import { isJsonArray, isJsonObject, pythonFloatRepr, type JsonObject, type JsonValue } from "./json.ts";
import type { ToolResult } from "./models.ts";
import { candidateSize, concatenatedText, fromPlain, select } from "./payload.ts";

export interface FinalResult {
  readonly kind: "result";
  readonly result: ToolResult;
  readonly structured?: JsonValue | null;
}

export interface InputRequired {
  readonly kind: "input_required";
  readonly result: Readonly<Record<string, unknown>>;
}

export type DownstreamReply = FinalResult | InputRequired;

export type PageCall = (args: JsonObject, signal: AbortSignal) => Promise<DownstreamReply>;

export const STATUS_COMPLETE = "complete";
export const STATUS_PARTIAL = "partial";
export const DETAIL_MAX = 200;

export const StopReason = {
  COMPLETE: "complete",
  MAX_PAGES: "max_pages",
  MAX_BYTES: "max_bytes",
  MAX_SECONDS: "max_seconds",
  PAGE_ERROR: "page_error",
  PAGE_FAILED: "page_failed",
  INTERACTIVE: "interactive",
  MALFORMED_PAGE: "malformed_page",
  MISSING_NEXT_OFFSET: "missing_next_offset",
  NOT_ADVANCING: "offset_not_advancing",
} as const;

export type StopReason = (typeof StopReason)[keyof typeof StopReason];

export class PaginationArgumentError extends Error {
  override readonly name = "PaginationArgumentError";
}

export interface FetchOutcome {
  readonly status: string;
  readonly reason: StopReason;
  readonly detail: string | null;
  readonly pages: number;
  readonly bytes: number;
  readonly seconds: number;
  readonly resumeOffset: bigint | null;
  readonly rows: readonly JsonValue[];
  readonly firstPageError: FinalResult | null;
}

interface Page {
  readonly items: readonly JsonValue[];
  readonly hasMore: boolean;
  readonly nextOffset: JsonValue | undefined;
}

export function structuredOf(reply: FinalResult): JsonValue | null {
  if (reply.structured !== undefined) {
    return reply.structured;
  }
  const plain = reply.result.structuredContent;
  return plain === undefined || plain === null ? null : fromPlain(plain);
}

function describe(value: JsonValue | undefined): string {
  if (value === undefined || value === null) {
    return "None";
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (typeof value === "number") {
    return pythonFloatRepr(value);
  }
  if (typeof value === "string") {
    return `'${value}'`;
  }
  return String(value);
}

export function splitArguments(config: PaginationConfig, args: JsonObject | null): readonly [JsonObject, bigint] {
  const filters = new Map(args ?? []);
  if (filters.has(config.limit_arg)) {
    throw new PaginationArgumentError(
      `\`${config.limit_arg}\` is managed by Sluice for this operation (page_size=${config.page_size}); pass filters only`,
    );
  }
  const offset = filters.get(config.offset_arg) ?? 0n;
  filters.delete(config.offset_arg);
  if (typeof offset !== "bigint" || offset < 0n) {
    throw new PaginationArgumentError(
      `\`${config.offset_arg}\` must be a non-negative integer to resume from, got ${describe(offset)}`,
    );
  }
  return [filters, offset];
}

function parsePage(config: PaginationConfig, reply: FinalResult): Page | string {
  const value = select(reply.result, structuredOf(reply)).value;
  if (!isJsonObject(value)) {
    return "page is not a JSON object";
  }
  const items = value.get(config.items);
  if (!isJsonArray(items)) {
    return `\`${config.items}\` is missing or not an array`;
  }
  const hasMore = value.get(config.has_more);
  if (typeof hasMore !== "boolean") {
    return `\`${config.has_more}\` is missing or not a boolean`;
  }
  return { items, hasMore, nextOffset: value.get(config.next_offset) };
}

function bounded(text: string): string {
  const points = [...text];
  return points.length <= DETAIL_MAX ? text : `${points.slice(0, DETAIL_MAX).join("")}...`;
}

class Expired extends Error {
  override readonly name = "Expired";
}

export async function fetchAll(
  call: PageCall,
  config: PaginationConfig,
  args: JsonObject | null,
  cancel?: AbortSignal,
): Promise<FetchOutcome> {
  const [filters, start] = splitArguments(config, args);
  let offset = start;
  const rows: JsonValue[] = [];
  const seen = new Set<bigint>();
  let pages = 0;
  let totalBytes = 0;
  let reason: StopReason = StopReason.COMPLETE;
  let detail: string | null = null;
  let resume: bigint | null = null;
  let firstPageError: FinalResult | null = null;
  const started = performance.now();
  const controller = new AbortController();
  let expire!: () => void;
  const deadline = new Promise<never>((_, reject) => {
    expire = () => {
      controller.abort(new Expired());
      reject(new Expired());
    };
  });
  deadline.catch(() => undefined);
  const cancelTimer = longTimeout(expire, config.max_seconds * 1000);
  const onCancel = (): void => controller.abort(cancel?.reason);
  cancel?.addEventListener("abort", onCancel, { once: true });
  try {
    for (;;) {
      cancel?.throwIfAborted();
      if (controller.signal.aborted) {
        throw new Expired();
      }
      if (pages >= config.max_pages) {
        [reason, detail, resume] = [StopReason.MAX_PAGES, `${pages} pages fetched`, offset];
        break;
      }
      const pageArgs = new Map(filters);
      pageArgs.set(config.limit_arg, BigInt(config.page_size));
      pageArgs.set(config.offset_arg, offset);
      let reply: DownstreamReply;
      try {
        const pending = call(pageArgs, controller.signal);
        pending.catch(() => undefined);
        reply = await Promise.race([pending, deadline]);
      } catch (error) {
        cancel?.throwIfAborted();
        if (controller.signal.aborted) {
          throw new Expired();
        }
        if (error instanceof DownstreamError) {
          [reason, detail, resume] = [StopReason.PAGE_FAILED, bounded(error.message), offset];
          break;
        }
        throw error;
      }
      if (reply.kind === "input_required") {
        reason = StopReason.INTERACTIVE;
        detail = "the tool requested input, which cannot be answered while paginating";
        resume = offset;
        break;
      }
      if (reply.result.isError === true) {
        reason = StopReason.PAGE_ERROR;
        detail = bounded(concatenatedText(reply.result)) || "downstream isError";
        resume = offset;
        if (pages === 0) {
          firstPageError = reply;
        }
        break;
      }
      const parsed = parsePage(config, reply);
      if (typeof parsed === "string") {
        [reason, detail, resume] = [StopReason.MALFORMED_PAGE, parsed, offset];
        break;
      }
      const pageBytes = candidateSize(reply.result, structuredOf(reply));
      if (totalBytes + pageBytes > config.max_bytes) {
        reason = StopReason.MAX_BYTES;
        detail =
          `page at offset ${offset} is ${pageBytes} bytes; ${totalBytes} already ` +
          `fetched, ceiling ${config.max_bytes}; the page was not kept`;
        resume = offset;
        break;
      }
      pages += 1;
      totalBytes += pageBytes;
      rows.push(...parsed.items);
      seen.add(offset);
      if (!parsed.hasMore) {
        break;
      }
      const next = parsed.nextOffset;
      if (typeof next !== "bigint") {
        reason = StopReason.MISSING_NEXT_OFFSET;
        detail = `\`${config.has_more}\` is true but \`${config.next_offset}\` is ${describe(next)}`;
        break;
      }
      const nextOffset = next;
      if (nextOffset <= offset || seen.has(nextOffset) || parsed.items.length === 0) {
        reason = StopReason.NOT_ADVANCING;
        detail = `offset ${offset} -> ${nextOffset} with ${parsed.items.length} items`;
        break;
      }
      offset = nextOffset;
    }
  } catch (error) {
    if (!(error instanceof Expired)) {
      throw error;
    }
    reason = StopReason.MAX_SECONDS;
    detail = `${config.max_seconds} s elapsed`;
    resume = offset;
  } finally {
    cancelTimer();
    cancel?.removeEventListener("abort", onCancel);
  }
  return {
    status: reason === StopReason.COMPLETE ? STATUS_COMPLETE : STATUS_PARTIAL,
    reason,
    detail,
    pages,
    bytes: totalBytes,
    seconds: (performance.now() - started) / 1000,
    resumeOffset: resume,
    rows,
    firstPageError,
  };
}
