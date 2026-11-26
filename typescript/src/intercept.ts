import { randomUUID } from "node:crypto";
import { toResult, renderPagination, sizeNote, type HandleResult } from "./handle.ts";
import { ColumnType, inferColumn, type ColumnSpec } from "./infer.ts";
import { parseJson, type JsonValue } from "./json.ts";
import type { Limits } from "./limits.ts";
import {
  Passthrough,
  PayloadChannel,
  type CallRecord,
  type ContentBlock,
  type Handle,
  type PaginationSummary,
  type SelectedPayload,
  type TablePlan,
  type TableRef,
  type ToolResult,
} from "./models.ts";
import { tableName } from "./naming.ts";
import {
  boundedPreviewRows,
  classifyPassthrough,
  contentKinds,
  fromPlain,
  metadataOnly,
  renderPreview,
  renderRowPreview,
  select,
} from "./payload.ts";
import { derive, mint } from "./scope.ts";
import { EXTRA_COLUMN, extract, project } from "./shape.ts";
import { Store, boundedFlatReason, retentionBudgetReason } from "./store.ts";
import { Semaphore } from "./sync.ts";

export type StructuredInput =
  | { readonly json: string }
  | { readonly value: JsonValue | null };

export interface InterceptRequest {
  readonly server: string;
  readonly tool: string;
  readonly mounted: string;
  readonly arguments: unknown;
  readonly result: ToolResult;
  readonly structured?: StructuredInput;
  readonly meta: unknown;
  readonly startedAt: Date;
  readonly failureClass?: string | null;
  readonly payloadCeiling?: number;
  readonly pagination?: PaginationSummary | null;
  readonly admitted?: boolean;
}

export type InterceptLogger = (event: string, error: unknown) => void;

export interface InterceptorOptions {
  readonly queryAvailable?: boolean;
  readonly logger?: InterceptLogger;
}

export function stderrLogger(event: string, error: unknown): void {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  process.stderr.write(`${JSON.stringify({ level: "error", logger: "sluice.intercept", event, detail })}\n`);
}

interface Plans {
  readonly plans: readonly TablePlan[];
  readonly tables: readonly TableRef[];
  readonly flatReason: string | null;
  readonly previewRows: readonly JsonValue[];
}

function describeError(error: unknown): string {
  if (error instanceof Error) {
    return `${error.name}: ${error.message}`;
  }
  return `Error: ${String(error)}`;
}

function resolveStructured(request: InterceptRequest): JsonValue | null {
  const input = request.structured;
  if (input === undefined) {
    const plain = request.result.structuredContent;
    return plain === undefined || plain === null ? null : fromPlain(plain);
  }
  if ("json" in input) {
    return parseJson(input.json);
  }
  return input.value;
}

function argumentsValue(args: unknown): JsonValue | null {
  if (args === null || args === undefined) {
    return null;
  }
  const value = args instanceof Map ? (args as JsonValue) : fromPlain(args);
  if (value instanceof Map && value.size === 0) {
    return null;
  }
  return value;
}

export class Interceptor {
  readonly #store: Store;
  readonly #limits: Limits;
  readonly #queryAvailable: boolean;
  readonly #admission: Semaphore;
  readonly #log: InterceptLogger;

  constructor(store: Store, limits: Limits, options: InterceptorOptions = {}) {
    this.#store = store;
    this.#limits = limits;
    this.#queryAvailable = options.queryAvailable ?? false;
    this.#log = options.logger ?? stderrLogger;
    this.#admission = new Semaphore(limits.max_concurrent_materializations);
  }

  withAdmission<T>(work: () => Promise<T>): Promise<T> {
    return this.#admission.run(work);
  }

  async intercept(request: InterceptRequest): Promise<ToolResult> {
    const run = async (): Promise<ToolResult> => {
      const retentionSeq = this.#store.nextRetentionSeq();
      try {
        return await this.#intercept(request, retentionSeq);
      } catch (error) {
        this.#log("intercept_failed", error);
        return request.result;
      } finally {
        this.#store.abandonCommit(retentionSeq);
      }
    };
    return request.admitted === true ? run() : this.withAdmission(run);
  }

  async #intercept(request: InterceptRequest, retentionSeq: number): Promise<ToolResult> {
    const { result } = request;
    const ceiling = request.payloadCeiling ?? this.#limits.max_payload_bytes;
    const pagination = request.pagination ?? null;
    const endedAt = new Date();
    const scopeId = this.#scope(request.meta);
    const seq = this.#store.nextCallSeq(request.mounted);
    const callId = randomUUID();

    let reason: Passthrough | null;
    let selected: SelectedPayload;
    let flatReason: string | null = null;
    try {
      const structured = resolveStructured(request);
      const classification = classifyPassthrough(result, structured, ceiling);
      reason = classification.reason;
      selected = reason === null ? select(result, structured) : metadataOnly(classification.size ?? 0);
    } catch (error) {
      this.#log("selection_failed", error);
      reason = Passthrough.SELECTION_FAILED;
      selected = metadataOnly();
      flatReason = boundedFlatReason(`selection_failed: ${describeError(error)}`);
    }

    let planned: Plans = { plans: [], tables: [], flatReason, previewRows: [] };
    if (reason === null) {
      planned = this.#plan(request.mounted, scopeId, selected);
    }
    let tables = planned.tables;

    let record: CallRecord = {
      callId,
      scopeId,
      seq,
      server: request.server,
      tool: request.tool,
      args: this.#arguments(request.arguments),
      payload: selected,
      isError: result.isError === true,
      failureClass: request.failureClass ?? (result.isError === true ? "tool_error" : null),
      contentKinds: contentKinds(result),
      startedAt: request.startedAt,
      endedAt,
      flatTables: tables.map((table) => table.name),
      sourcePaths: tables.map((table) => table.sourcePath),
      flatReason: boundedFlatReason(planned.flatReason),
      retentionSeq,
    };

    try {
      const retained = await this.#store.commitCall(record, planned.plans);
      if (!retained) {
        tables = [];
        record = { ...record, flatTables: [], sourcePaths: [], flatReason: retentionBudgetReason(record.flatReason) };
      }
    } catch (error) {
      this.#log("commit_failed", error);
      tables = [];
      const degraded: CallRecord = {
        ...record,
        flatTables: [],
        sourcePaths: [],
        flatReason: boundedFlatReason(`load_failed: ${describeError(error)}`),
      };
      try {
        const retained = await this.#store.commitCall(degraded, []);
        record = retained ? degraded : { ...degraded, flatReason: retentionBudgetReason(degraded.flatReason) };
      } catch (envelopeError) {
        this.#log("envelope_payload_failed", envelopeError);
        this.#store.abandonCommit(retentionSeq);
        const metadata: CallRecord = {
          ...degraded,
          args: null,
          payload: { ...degraded.payload, value: undefined, text: null, blocks: [], structured: null },
          flatReason: boundedFlatReason(`load_failed: ${describeError(error)}; payload_dropped: ${describeError(envelopeError)}`),
          retentionSeq: 0,
        };
        try {
          await this.#store.commitCall(metadata, []);
          record = metadata;
        } catch (metadataError) {
          this.#log("envelope_write_failed", metadataError);
          return result;
        }
      }
    }

    if (reason !== null) {
      return this.#passthrough(result, reason, selected, ceiling, pagination);
    }
    try {
      return this.#handleResult(record, selected, tables, planned.previewRows, pagination);
    } catch (error) {
      this.#log("handle_failed", error);
      return result;
    }
  }

  #scope(meta: unknown): string {
    try {
      return derive(meta).tag;
    } catch (error) {
      this.#log("scope_unreadable", error);
      return mint();
    }
  }

  #arguments(args: unknown): JsonValue | null {
    try {
      return argumentsValue(args);
    } catch (error) {
      this.#log("arguments_unrecorded", error);
      return null;
    }
  }

  #passthrough(
    result: ToolResult,
    reason: Passthrough,
    selected: SelectedPayload,
    ceiling: number,
    pagination: PaginationSummary | null,
  ): ToolResult {
    const notes: ContentBlock[] = [];
    if (reason === Passthrough.OVERSIZE) {
      notes.push({ type: "text", text: sizeNote(selected.byteSize, ceiling) });
    }
    if (pagination !== null) {
      notes.push({ type: "text", text: renderPagination(pagination) });
    }
    if (notes.length === 0) {
      return result;
    }
    return { ...result, content: [...(Array.isArray(result.content) ? result.content : []), ...notes] };
  }

  #plan(mounted: string, scopeId: string, selected: SelectedPayload): Plans {
    if (selected.channel === PayloadChannel.NONE || selected.value === undefined) {
      return { plans: [], tables: [], flatReason: "not_json: no channel parsed as JSON", previewRows: [] };
    }
    try {
      return this.#buildPlans(mounted, scopeId, selected.value);
    } catch (error) {
      this.#log("planning_failed", error);
      return {
        plans: [],
        tables: [],
        flatReason: boundedFlatReason(`load_failed: ${describeError(error)}`),
        previewRows: [],
      };
    }
  }

  #buildPlans(mounted: string, scopeId: string, value: JsonValue): Plans {
    const extraction = extract(value);
    if (extraction.reason !== null) {
      return { plans: [], tables: [], flatReason: extraction.reason, previewRows: [] };
    }
    const plans: TablePlan[] = [];
    const tables: TableRef[] = [];
    let previewRows: JsonValue[] = [];
    for (const rowSet of extraction.rowSets) {
      if (plans.length === 0) {
        previewRows = boundedPreviewRows(rowSet.rows, this.#limits.preview_rows, this.#limits.preview_bytes);
      }
      const projection = project(rowSet.rows, this.#limits.max_columns);
      const storedToSource = new Map([...projection.renamed].map(([source, stored]) => [stored, source]));
      const specs: ColumnSpec[] = projection.columns.map((column) => {
        const inference =
          column === EXTRA_COLUMN
            ? { type: ColumnType.JSON, exact: false }
            : inferColumn(projection.records.map((record) => record.get(column) ?? null));
        return { name: column, ...inference, renamedFrom: storedToSource.get(column) ?? null };
      });
      const name = tableName(mounted, scopeId, this.#store.nextTableSeq(mounted));
      plans.push({ name, sourcePath: rowSet.sourcePath, columns: specs, records: projection.records });
      tables.push({
        name,
        sourcePath: rowSet.sourcePath,
        rowCount: projection.records.length,
        columns: specs.map((spec) => ({
          name: spec.name,
          type: spec.type,
          exact: spec.exact,
          renamedFrom: spec.renamedFrom,
        })),
      });
    }
    return { plans, tables, flatReason: null, previewRows };
  }

  #handleResult(
    record: CallRecord,
    selected: SelectedPayload,
    tables: readonly TableRef[],
    previewRows: readonly JsonValue[],
    pagination: PaginationSummary | null,
  ): HandleResult {
    let [preview, complete] = renderPreview(selected, this.#limits.preview_bytes);
    let shown: number | null = null;
    let total: number | null = null;
    const [first] = tables;
    if (!complete && first !== undefined) {
      [preview, shown] = renderRowPreview(previewRows, previewRows.length, this.#limits.preview_bytes);
      total = first.rowCount;
    }
    const handle: Handle = {
      callId: record.callId,
      scopeId: record.scopeId,
      channel: selected.channel,
      conflict: selected.conflict,
      byteSize: selected.byteSize,
      preview,
      previewComplete: complete,
      previewRows: shown,
      totalRows: total,
      tables,
      flatReason: record.flatReason,
      pagination,
      queryAvailable: this.#queryAvailable,
    };
    return toResult(handle);
  }
}
