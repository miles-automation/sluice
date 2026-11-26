import type { ContentBlock, Handle, PaginationSummary, TableRef } from "./models.ts";
import { scopeViewName } from "./store.ts";

export const QUERY_HINT = "Run SQL over this with the `query` tool.";

export interface HandleResult {
  readonly [field: string]: unknown;
  readonly content: readonly ContentBlock[];
  readonly structuredContent: StructuredHandle;
}

export interface StructuredColumn {
  readonly name: string;
  readonly type: string;
  readonly exact: boolean;
  readonly renamed_from: string | null;
}

export interface StructuredTable {
  readonly name: string;
  readonly source_path: string;
  readonly row_count: number;
  readonly columns: readonly StructuredColumn[];
}

export interface StructuredPagination {
  readonly status: string;
  readonly reason: string;
  readonly detail: string | null;
  readonly pages: number;
  readonly rows: number;
  readonly bytes: number;
  readonly seconds: number;
  readonly resume_offset: number | null;
}

export interface StructuredHandle {
  readonly [key: string]: unknown;
  readonly call_id: string;
  readonly scope_id: string;
  readonly envelope_table: string;
  readonly source_channel: string;
  readonly channel_conflict: boolean;
  readonly tables: readonly StructuredTable[];
  readonly flat_reason: string | null;
  readonly pagination: StructuredPagination | null;
  readonly byte_size: number;
  readonly preview_complete: boolean;
  readonly preview: string;
}

const LINE_BREAK = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/u;

export function splitLines(text: string): string[] {
  const lines = text.split(LINE_BREAK);
  if (lines.at(-1) === "") {
    lines.pop();
  }
  return lines;
}

function formatColumns(table: TableRef): string[] {
  return table.columns.map((column) => {
    const marker = column.type === "JSON" ? "*" : "";
    const suffix = column.exact ? "" : " (inexact)";
    return `${column.name} ${column.type}${marker}${suffix}`;
  });
}

function indent(text: string): string {
  const lines = splitLines(text);
  return (lines.length > 0 ? lines : [""]).map((line) => `  ${line}`).join("\n");
}

function paginationLines(summary: PaginationSummary): string[] {
  const counts = `pages=${summary.pages}  rows=${summary.rows}  bytes=${summary.bytes}  seconds=${summary.seconds.toFixed(1)}`;
  if (summary.status === "complete") {
    return [`pagination: complete  ${counts}`];
  }
  const lines = [`pagination: PARTIAL, stopped by ${summary.reason}: ${summary.detail ?? "None"}  ${counts}`];
  if (summary.resumeOffset !== null) {
    lines.push(
      `  rows from offset ${summary.resumeOffset} onward were NOT fetched; call again with offset=${summary.resumeOffset} to continue.`,
    );
  } else {
    lines.push("  the remainder cannot be resumed safely from this response.");
  }
  return lines;
}

export function renderPagination(summary: PaginationSummary): string {
  return paginationLines(summary).join("\n");
}

export function renderText(handle: Handle): string {
  const lines = [`sluice: result recorded.  channel=${handle.channel}  scope=${handle.scopeId}`];
  if (handle.pagination !== null) {
    lines.push(...paginationLines(handle.pagination));
  }
  if (handle.conflict) {
    lines.push(
      "warning: structuredContent and the text content both parsed and disagree. " +
        "The structured channel was used; the text channel is in result_text.",
    );
  }
  for (const table of handle.tables) {
    lines.push(`table: ${table.name}   rows=${table.rowCount}   from=${table.sourcePath}`);
    const columns = formatColumns(table);
    if (columns.length > 0) {
      lines.push(`columns: ${columns.join(", ")}`);
    }
    if (table.columns.some((column) => column.type === "JSON")) {
      lines.push("         (* JSON: use json_extract and cast before arithmetic)");
    }
    const renamed = table.columns.filter((column) => column.renamedFrom !== null);
    if (renamed.length > 0) {
      lines.push(`renamed: ${renamed.map((column) => `${String(column.renamedFrom)} -> ${column.name}`).join(", ")}`);
    }
  }
  if (handle.tables.length === 0) {
    lines.push(`no table: ${handle.flatReason ?? "no tabular rows found"}`);
  }
  lines.push(`envelope: ${scopeViewName(handle.scopeId)} WHERE call_id = '${handle.callId}'`);
  if (handle.previewComplete) {
    lines.push(`preview (complete, ${handle.byteSize} B):`);
  } else if (handle.previewRows !== null && handle.totalRows !== null) {
    lines.push(`preview (first ${handle.previewRows} of ${handle.totalRows} rows, payload ${handle.byteSize} B):`);
  } else {
    lines.push(`preview (truncated, of ${handle.byteSize} B):`);
  }
  lines.push(indent(handle.preview));
  if (handle.queryAvailable) {
    lines.push(QUERY_HINT);
  }
  return lines.join("\n");
}

export function paginationObject(summary: PaginationSummary): StructuredPagination {
  return {
    status: summary.status,
    reason: summary.reason,
    detail: summary.detail,
    pages: summary.pages,
    rows: summary.rows,
    bytes: summary.bytes,
    seconds: Math.round(summary.seconds * 1000) / 1000,
    resume_offset: summary.resumeOffset,
  };
}

export function renderStructured(handle: Handle): StructuredHandle {
  return {
    call_id: handle.callId,
    scope_id: handle.scopeId,
    envelope_table: scopeViewName(handle.scopeId),
    source_channel: handle.channel,
    channel_conflict: handle.conflict,
    tables: handle.tables.map((table) => ({
      name: table.name,
      source_path: table.sourcePath,
      row_count: table.rowCount,
      columns: table.columns.map((column) => ({
        name: column.name,
        type: column.type,
        exact: column.exact,
        renamed_from: column.renamedFrom,
      })),
    })),
    flat_reason: handle.flatReason,
    pagination: handle.pagination === null ? null : paginationObject(handle.pagination),
    byte_size: handle.byteSize,
    preview_complete: handle.previewComplete,
    preview: handle.preview,
  };
}

export function toResult(handle: Handle): HandleResult {
  return {
    content: [{ type: "text", text: renderText(handle) }],
    structuredContent: renderStructured(handle),
  };
}

export function sizeNote(byteSize: number, limit: number): string {
  return (
    `sluice: result passed through unmodified. It is ${byteSize} bytes, over the ` +
    `${limit}-byte materialization ceiling, so it was not parsed or stored.`
  );
}
