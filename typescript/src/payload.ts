import { dumpJson, isJsonArray, isJsonObject, parseJson, type JsonObject, type JsonValue } from "./json.ts";
import {
  Passthrough,
  PayloadChannel,
  type BlockDescription,
  type ContentBlock,
  type SelectedPayload,
  type ToolResult,
} from "./models.ts";

export const TEXT_KIND = "text";
export const MAX_CONTENT_KINDS = 32;
export const MAX_CONTENT_KIND_CHARS = 64;

const ENCODER = new TextEncoder();

export function byteLength(text: string): number {
  return ENCODER.encode(text).length;
}

export function serializedSize(value: JsonValue): number {
  return byteLength(dumpJson(value));
}

export function fromPlain(value: unknown): JsonValue {
  if (value === null || value === undefined) {
    return null;
  }
  switch (typeof value) {
    case "boolean":
    case "string":
    case "bigint":
      return value;
    case "number":
      return Number.isSafeInteger(value) ? BigInt(value) : value;
    case "object":
      break;
    default:
      return String(value);
  }
  if (value instanceof Map) {
    const entries = new Map<string, JsonValue>();
    for (const [key, item] of value as Map<unknown, unknown>) {
      entries.set(String(key), fromPlain(item));
    }
    return entries;
  }
  if (Array.isArray(value)) {
    return value.map(fromPlain);
  }
  const entries = new Map<string, JsonValue>();
  for (const [key, item] of Object.entries(value)) {
    if (item !== undefined) {
      entries.set(key, fromPlain(item));
    }
  }
  return entries;
}

export function isTextBlock(block: ContentBlock): block is ContentBlock & { readonly text: string } {
  return block.type === TEXT_KIND && typeof block.text === "string";
}

export function textBlocks(result: ToolResult): readonly string[] {
  return result.content.filter(isTextBlock).map((block) => block.text);
}

export function contentKinds(result: ToolResult): string[] {
  const kinds = result.content
    .slice(0, MAX_CONTENT_KINDS)
    .map((block) => Array.from(typeof block.type === "string" ? block.type : "unknown").slice(0, MAX_CONTENT_KIND_CHARS).join(""));
  if (result.content.length > MAX_CONTENT_KINDS) {
    kinds.push("truncated");
  }
  return kinds;
}

export function describeBlocks(result: ToolResult): BlockDescription[] {
  return result.content.map((block) =>
    isTextBlock(block) ? { type: block.type, text: block.text } : { type: typeof block.type === "string" ? block.type : "unknown" },
  );
}

export function describedValue(blocks: readonly BlockDescription[]): JsonValue {
  return blocks.map((block) => {
    const entry = new Map<string, JsonValue>([["type", block.type]]);
    if (block.text !== undefined) {
      entry.set("text", block.text);
    }
    return entry;
  });
}

export function metadataOnly(byteSize = 0): SelectedPayload {
  return {
    channel: PayloadChannel.NONE,
    value: undefined,
    text: null,
    blocks: [],
    structured: null,
    byteSize,
    wireBytes: null,
    conflict: false,
  };
}

export function concatenatedText(result: ToolResult): string {
  return textBlocks(result).join("");
}

export function candidateSize(result: ToolResult, structured: JsonValue | null): number {
  const structuredBytes = structured === null ? 0 : serializedSize(structured);
  return textBlocks(result).reduce((total, text) => total + byteLength(text), structuredBytes);
}

export interface Classification {
  readonly reason: Passthrough | null;
  readonly size: number | null;
}

export function classifyPassthrough(
  result: ToolResult,
  structured: JsonValue | null,
  maxPayloadBytes: number,
): Classification {
  if (result.isError === true) {
    return { reason: Passthrough.ERROR, size: null };
  }
  if (result.content.some((block) => block.type !== TEXT_KIND)) {
    return { reason: Passthrough.NON_TEXT, size: null };
  }
  const size = candidateSize(result, structured);
  return { reason: size > maxPayloadBytes ? Passthrough.OVERSIZE : null, size };
}

export function wireBytes(result: ToolResult, structured: JsonValue | null): number {
  const { structuredContent: _, ...rest } = result;
  const envelope = byteLength(JSON.stringify(rest, (_key, value: unknown) => (typeof value === "bigint" ? value.toString() : value)));
  return structured === null ? envelope : envelope + serializedSize(structured);
}

type Parsed = { readonly ok: true; readonly value: JsonValue } | { readonly ok: false };

function parse(text: string): Parsed {
  try {
    return { ok: true, value: parseJson(text) };
  } catch (error) {
    if (error instanceof RangeError) {
      throw error;
    }
    return { ok: false };
  }
}

function textPayload(result: ToolResult): Parsed {
  const successes = textBlocks(result)
    .map(parse)
    .filter((parsed) => parsed.ok);
  const [only] = successes;
  if (successes.length === 1 && only !== undefined) {
    return only;
  }
  return parse(concatenatedText(result));
}

function numeric(value: JsonValue): bigint | number | undefined {
  if (typeof value === "boolean") {
    return value ? 1n : 0n;
  }
  if (typeof value === "bigint" || typeof value === "number") {
    return value;
  }
  return undefined;
}

function numbersEqual(a: bigint | number, b: bigint | number): boolean {
  if (typeof a === "bigint" && typeof b === "bigint") {
    return a === b;
  }
  if (typeof a === "number" && typeof b === "number") {
    return a === b;
  }
  const [integer, float] = typeof a === "bigint" ? [a, b as number] : [b as bigint, a];
  return Number.isInteger(float) && BigInt(float) === integer;
}

export function pythonEquals(a: JsonValue, b: JsonValue): boolean {
  const left = numeric(a);
  const right = numeric(b);
  if (left !== undefined || right !== undefined) {
    return left !== undefined && right !== undefined && numbersEqual(left, right);
  }
  if (a === null || b === null || typeof a === "string" || typeof b === "string") {
    return a === b;
  }
  if (isJsonArray(a) || isJsonArray(b)) {
    return isJsonArray(a) && isJsonArray(b) && a.length === b.length && a.every((item, index) => pythonEquals(item, b[index] ?? null));
  }
  if (!isJsonObject(a) || !isJsonObject(b)) {
    return false;
  }
  const objectA: JsonObject = a;
  const objectB: JsonObject = b;
  if (objectA.size !== objectB.size) {
    return false;
  }
  for (const [key, item] of objectA) {
    const other = objectB.get(key);
    if (other === undefined || !pythonEquals(item, other)) {
      return false;
    }
  }
  return true;
}

export function select(result: ToolResult, structured: JsonValue | null): SelectedPayload {
  const text = concatenatedText(result);
  const blocks = describeBlocks(result);
  const wire = wireBytes(result, structured);
  const parsedText = textPayload(result);
  if (structured !== null) {
    return {
      channel: PayloadChannel.STRUCTURED,
      value: structured,
      text,
      blocks,
      structured,
      byteSize: serializedSize(structured),
      wireBytes: wire,
      conflict: parsedText.ok && !pythonEquals(parsedText.value, structured),
    };
  }
  if (parsedText.ok) {
    return {
      channel: PayloadChannel.TEXT,
      value: parsedText.value,
      text,
      blocks,
      structured: null,
      byteSize: byteLength(text),
      wireBytes: wire,
      conflict: false,
    };
  }
  return {
    channel: PayloadChannel.NONE,
    value: undefined,
    text,
    blocks,
    structured: null,
    byteSize: byteLength(text),
    wireBytes: wire,
    conflict: false,
  };
}

export function truncateToBytes(text: string, limit: number): readonly [string, boolean] {
  const encoded = ENCODER.encode(text);
  if (encoded.length <= limit) {
    return [text, false];
  }
  let cut = Math.max(limit, 0);
  while (cut > 0 && ((encoded[cut] ?? 0) & 0xc0) === 0x80) {
    cut -= 1;
  }
  return [new TextDecoder().decode(encoded.subarray(0, cut)), true];
}

export function renderPreview(payload: SelectedPayload, limit: number): readonly [string, boolean] {
  const source = payload.channel === PayloadChannel.NONE || payload.value === undefined ? (payload.text ?? "") : dumpJson(payload.value);
  const [truncated, cut] = truncateToBytes(source, limit);
  return [truncated, !cut];
}

export function renderRowPreview(rows: readonly JsonValue[], count: number, limit: number): readonly [string, number] {
  const shown = rows.slice(0, count);
  const [truncated] = truncateToBytes(shown.map(dumpJson).join("\n"), limit);
  return [truncated, shown.length];
}

export function boundedPreviewRows(rows: readonly JsonValue[], count: number, limit: number): JsonValue[] {
  const selected: JsonValue[] = [];
  let used = 0;
  for (const row of rows.slice(0, count)) {
    const size = serializedSize(row);
    const separator = selected.length > 0 ? 1 : 0;
    if (used + separator + size > limit) {
      break;
    }
    selected.push(row);
    used += separator + size;
  }
  return selected;
}

