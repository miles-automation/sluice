import type { JsonValue } from "./json.ts";
import type { ColumnSpec } from "./infer.ts";
import type { ProjectedRecord } from "./shape.ts";

export const PayloadChannel = {
  STRUCTURED: "structured",
  TEXT: "text",
  NONE: "none",
} as const;

export type PayloadChannel = (typeof PayloadChannel)[keyof typeof PayloadChannel];

export const Passthrough = {
  ERROR: "is_error",
  NON_TEXT: "non_text_content",
  OVERSIZE: "oversize",
  SELECTION_FAILED: "selection_failed",
} as const;

export type Passthrough = (typeof Passthrough)[keyof typeof Passthrough];

export interface ContentBlock {
  readonly type: string;
  readonly text?: string;
  readonly [field: string]: unknown;
}

export interface ToolResult {
  readonly content: readonly ContentBlock[];
  readonly structuredContent?: { readonly [key: string]: unknown } | undefined;
  readonly isError?: boolean | undefined;
  readonly [field: string]: unknown;
}

export interface BlockDescription {
  readonly type: string;
  readonly text?: string;
}

export interface ColumnRef {
  readonly name: string;
  readonly type: string;
  readonly exact: boolean;
  readonly renamedFrom: string | null;
}

export interface TableRef {
  readonly name: string;
  readonly sourcePath: string;
  readonly rowCount: number;
  readonly columns: readonly ColumnRef[];
}

export interface TablePlan {
  readonly name: string;
  readonly sourcePath: string;
  readonly columns: readonly ColumnSpec[];
  readonly records: readonly ProjectedRecord[];
}

export interface SelectedPayload {
  readonly channel: PayloadChannel;
  readonly value: JsonValue | undefined;
  readonly text: string | null;
  readonly blocks: readonly BlockDescription[];
  readonly structured: JsonValue | null;
  readonly byteSize: number;
  readonly wireBytes: number | null;
  readonly conflict: boolean;
}

export interface CallRecord {
  readonly callId: string;
  readonly scopeId: string;
  readonly seq: number;
  readonly server: string;
  readonly tool: string;
  readonly args: JsonValue | null;
  readonly payload: SelectedPayload;
  readonly isError: boolean;
  readonly failureClass: string | null;
  readonly contentKinds: readonly string[];
  readonly startedAt: Date;
  readonly endedAt: Date;
  readonly flatTables: readonly string[];
  readonly sourcePaths: readonly string[];
  readonly flatReason: string | null;
  readonly retentionSeq: number;
}

export interface PaginationSummary {
  readonly status: string;
  readonly reason: string;
  readonly detail: string | null;
  readonly pages: number;
  readonly rows: number;
  readonly bytes: number;
  readonly seconds: number;
  readonly resumeOffset: number | null;
}

export interface Handle {
  readonly callId: string;
  readonly scopeId: string;
  readonly channel: PayloadChannel;
  readonly conflict: boolean;
  readonly byteSize: number;
  readonly preview: string;
  readonly previewComplete: boolean;
  readonly previewRows: number | null;
  readonly totalRows: number | null;
  readonly tables: readonly TableRef[];
  readonly flatReason: string | null;
  readonly pagination: PaginationSummary | null;
  readonly queryAvailable: boolean;
}

export function durationMs(record: CallRecord): number {
  return Math.trunc(record.endedAt.getTime() - record.startedAt.getTime());
}
