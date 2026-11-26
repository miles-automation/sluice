import assert from "node:assert/strict";
import type { Case, Json, Kind } from "./fixtures.ts";
import { inferColumn } from "../../src/infer.ts";
import { isJsonArray, parseJson, type JsonObject, type JsonValue } from "../../src/json.ts";
import {
  assertInjective,
  collectionName,
  mountedName,
  quoteIdent,
  slug,
  tableName,
  tag,
  type ToolPair,
} from "../../src/naming.ts";
import { derive, fromConversationId, mint } from "../../src/scope.ts";
import { extract, project } from "../../src/shape.ts";
import { runCell, runEngine, runGate, runGateAst, runSession } from "./duckdb-adapters.ts";
import { runConfig, runPaginate } from "./proxy-adapters.ts";

export type Adapter = (testCase: Case) => void | Promise<void>;

export interface Pending {
  readonly pending: string;
}

type JsonRecord = { readonly [key: string]: Json };

function isRecord(value: Json | undefined): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: Json | undefined, what: string): JsonRecord {
  assert.ok(isRecord(value), `${what} must be an object`);
  return value;
}

function text(value: Json | undefined, what: string): string {
  assert.equal(typeof value, "string", `${what} must be a string`);
  return value as string;
}

function integer(value: Json | undefined, what: string): number {
  assert.ok(typeof value === "number" && Number.isSafeInteger(value), `${what} must be an integer`);
  return value;
}

function list(value: Json | undefined, what: string): readonly Json[] {
  assert.ok(Array.isArray(value), `${what} must be a list`);
  return value;
}

export function toPlain(value: JsonValue): Json {
  if (typeof value === "bigint") {
    assert.ok(
      value >= BigInt(Number.MIN_SAFE_INTEGER) && value <= BigInt(Number.MAX_SAFE_INTEGER),
      `integer ${value} has no plain JSON form`,
    );
    return Number(value);
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (isJsonArray(value)) {
    return value.map(toPlain);
  }
  const plain: { [key: string]: Json } = {};
  for (const [key, item] of value) {
    plain[key] = toPlain(item);
  }
  return plain;
}

function rowsOf(value: JsonValue): readonly JsonObject[] {
  assert.ok(isJsonArray(value), "rows_json must be a list");
  return value.map((row) => {
    assert.ok(row instanceof Map, "every row must be an object");
    return row;
  });
}

function runExtract(testCase: Case): void {
  const result = extract(parseJson(text(testCase["payload_json"], "payload_json")));
  const expect = record(testCase["expect"], "expect");
  if ("row_sets" in expect) {
    assert.equal(result.reason, null);
    assert.deepEqual(
      result.rowSets.map((rowSet) => ({ source_path: rowSet.sourcePath, rows: rowSet.rows.map(toPlain) })),
      expect["row_sets"],
    );
    return;
  }
  assert.deepEqual(result.rowSets, []);
  assert.ok(result.reason !== null);
  assert.equal(result.reason.split(":", 1)[0], expect["reason"]);
  for (const path of list(expect["paths"] ?? [], "paths")) {
    assert.ok(result.reason.includes(text(path, "path")), `reason ${result.reason} does not name ${String(path)}`);
  }
}

function runProject(testCase: Case): void {
  const projection = project(
    rowsOf(parseJson(text(testCase["rows_json"], "rows_json"))),
    integer(testCase["max_columns"], "max_columns"),
  );
  const expect = record(testCase["expect"], "expect");
  if ("columns" in expect) {
    assert.deepEqual(projection.columns, expect["columns"]);
  }
  if ("columns_prefix" in expect) {
    const prefix = list(expect["columns_prefix"], "columns_prefix");
    assert.deepEqual(projection.columns.slice(0, prefix.length), prefix);
  }
  if ("column_count" in expect) {
    assert.equal(projection.columns.length, expect["column_count"]);
  }
  if ("renamed" in expect) {
    assert.deepEqual(Object.fromEntries(projection.renamed), expect["renamed"]);
  }
  if ("extra_keys" in expect) {
    assert.deepEqual(projection.extraKeys, expect["extra_keys"]);
  }
  if ("records" in expect) {
    assert.deepEqual(projection.records.map(toPlain), expect["records"]);
  }
}

function runInfer(testCase: Case): void {
  const values = parseJson(text(testCase["values_json"], "values_json"));
  assert.ok(isJsonArray(values), "values_json must be a list");
  const expect = record(testCase["expect"], "expect");
  assert.deepEqual(inferColumn(values), { type: expect["type"], exact: expect["exact"] });
}

function pair(value: Json): ToolPair {
  const items = list(value, "pair");
  return [text(items[0], "server"), text(items[1], "tool")];
}

function naming(op: string, args: JsonRecord): string | boolean | ReadonlyMap<string, ToolPair> {
  switch (op) {
    case "slug":
      return slug(text(args["value"], "value"));
    case "tag":
      return tag(text(args["value"], "value"));
    case "mounted_name":
      return mountedName(text(args["server"], "server"), text(args["tool"], "tool"));
    case "collection_name":
      return collectionName(text(args["server"], "server"), text(args["tool"], "tool"));
    case "table_name":
      return tableName(
        mountedName(text(args["server"], "server"), text(args["tool"], "tool")),
        text(args["scope"], "scope"),
        integer(args["seq"], "seq"),
      );
    case "quote_ident":
      return quoteIdent(text(args["value"], "value"));
    case "assert_injective":
      return assertInjective(list(args["pairs"], "pairs").map(pair));
    case "distinct": {
      const names = list(args["items"], "items").map((item) => {
        const entry = record(item, "item");
        return naming(text(entry["op"], "op"), record(entry["args"], "args"));
      });
      return new Set(names).size === names.length;
    }
    default:
      throw new Error(`unknown naming op ${op}`);
  }
}

function runNaming(testCase: Case): void {
  const value = naming(text(testCase["op"], "op"), record(testCase["args"], "args"));
  const expect = record(testCase["expect"], "expect");
  const asText = (): string => text(typeof value === "string" ? value : undefined, "result");
  if ("value" in expect) {
    assert.equal(value, expect["value"]);
  }
  if ("prefix" in expect) {
    assert.ok(asText().startsWith(text(expect["prefix"], "prefix")));
  }
  if ("suffix" in expect) {
    assert.ok(asText().endsWith(text(expect["suffix"], "suffix")));
  }
  if ("max_length" in expect) {
    assert.ok(asText().length <= integer(expect["max_length"], "max_length"));
  }
  if ("distinct" in expect) {
    assert.equal(value, expect["distinct"]);
  }
  if ("count" in expect) {
    assert.ok(value instanceof Map, "count applies to assert_injective");
    assert.equal(value.size, expect["count"]);
  }
}

function runScope(testCase: Case): void {
  const expect = record(testCase["expect"], "expect");
  const pattern = "pattern" in expect ? new RegExp(`^(?:${text(expect["pattern"], "pattern")})$`, "u") : null;
  const op = text(testCase["op"], "op");
  if (op === "from_conversation_id") {
    assert.equal(fromConversationId(text(testCase["conversation_id"], "conversation_id")), expect["scope"]);
    return;
  }
  if (op === "mint") {
    const count = integer(expect["distinct_over"], "distinct_over");
    const minted = new Set(Array.from({ length: count }, mint));
    assert.equal(minted.size, count);
    for (const scope of minted) {
      assert.match(scope, pattern ?? /^/u);
    }
    return;
  }
  assert.equal(op, "derive");
  const meta = testCase["meta"];
  const derived = derive(meta);
  assert.equal(derived.fromClient, expect["from_client"]);
  if ("scope" in expect) {
    assert.equal(derived.tag, expect["scope"]);
  }
  if (pattern !== null) {
    assert.match(derived.tag, pattern);
  }
  if ("distinct_over" in expect) {
    const count = integer(expect["distinct_over"], "distinct_over");
    const tags = new Set(Array.from({ length: count }, () => derive(meta).tag));
    assert.equal(tags.size, count);
  }
}

export const adapters: Readonly<Record<Kind, Adapter | Pending>> = {
  extract: runExtract,
  project: runProject,
  infer: runInfer,
  naming: runNaming,
  scope: runScope,
  cell: runCell,
  gate: runGate,
  gate_ast: runGateAst,
  engine: runEngine,
  session: runSession,
  paginate: runPaginate,
  config: runConfig,
};
