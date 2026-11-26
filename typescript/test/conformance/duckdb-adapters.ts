import assert from "node:assert/strict";
import { DuckDBInstance, type DuckDBConnection } from "@duckdb/node-api";
import type { Case, Json } from "./fixtures.ts";
import { matchValue } from "./matchers.ts";
import { applyMutations } from "./mutations.ts";
import { LOCKDOWN, assertQueryView, assertRejection, parseQueryOutput } from "./runtime.ts";
import { QueryRejectedError, check, prepareSelect, serializeSql } from "../../src/gate.ts";
import { Interceptor } from "../../src/intercept.ts";
import { makeLimits, type Limits } from "../../src/limits.ts";
import type { ContentBlock, ToolResult } from "../../src/models.ts";
import { mountedName } from "../../src/naming.ts";
import { QueryTool, escapeCell, runQueryTool } from "../../src/query.ts";
import { derive } from "../../src/scope.ts";
import { Store, scopeViewName } from "../../src/store.ts";

type JsonRecord = { readonly [key: string]: Json };

function isRecord(value: Json | undefined): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: Json | undefined, what: string): JsonRecord {
  assert.ok(isRecord(value), `${what} must be an object`);
  return value;
}

function list(value: Json | undefined): readonly Json[] {
  return Array.isArray(value) ? value : [];
}

function strings(value: Json | undefined): string[] {
  return list(value).map(String);
}

async function withConnection<T>(work: (connection: DuckDBConnection) => Promise<T>): Promise<T> {
  const instance = await DuckDBInstance.create(":memory:");
  const connection = await instance.connect();
  try {
    return await work(connection);
  } finally {
    connection.closeSync();
    instance.closeSync();
  }
}

export function runCell(testCase: Case): void {
  const expect = record(testCase["expect"], "expect");
  const maxBytes = Number(testCase["max_bytes"]);
  const [text, truncated] = escapeCell(testCase["value"], maxBytes);
  assert.deepEqual([text, truncated], [expect["text"], expect["truncated"]]);
  if (truncated) {
    assert.ok(new TextEncoder().encode(text).length <= maxBytes);
  }
}

async function prepareGateConnection(testCase: Case, connection: DuckDBConnection): Promise<void> {
  for (const statement of strings(testCase["setup"])) {
    await connection.run(statement);
  }
  for (const statement of LOCKDOWN) {
    await connection.run(statement);
  }
}

async function assertGate(
  testCase: Case,
  connection: DuckDBConnection,
  serialize?: (sql: string) => Promise<unknown>,
): Promise<void> {
  const expect = record(testCase["expect"], "expect");
  const sql = String(testCase["sql"]);
  const allowed = strings(testCase["allowed"]);
  const evicted = strings(testCase["evicted"]);
  const options = serialize === undefined ? {} : { serialize };
  if (expect["decision"] === "allow") {
    await check(sql, connection, allowed, evicted, options);
    const prepared = await prepareSelect(sql, connection);
    try {
      if (expect["executes"] === true) {
        await prepared.runAndReadAll();
      }
    } finally {
      prepared.destroySync();
    }
    return;
  }
  let rejection: unknown;
  try {
    await check(sql, connection, allowed, evicted, options);
    const prepared = await prepareSelect(sql, connection);
    prepared.destroySync();
  } catch (error) {
    rejection = error;
  }
  assert.ok(rejection instanceof QueryRejectedError, `expected a rejection, got ${String(rejection)}`);
  assertRejection(expect, rejection.message);
}

export async function runGate(testCase: Case): Promise<void> {
  await withConnection(async (connection) => {
    await prepareGateConnection(testCase, connection);
    await assertGate(testCase, connection);
  });
}

function mutationSql(mutations: readonly Json[]): string[] {
  return mutations.filter(isRecord).flatMap((mutation) =>
    mutation["op"] === "replace_serialized" || mutation["op"] === "set_serialized" ? [String(mutation["sql"])] : [],
  );
}

export async function runGateAst(testCase: Case): Promise<void> {
  await withConnection(async (connection) => {
    await prepareGateConnection(testCase, connection);
    const mutations = list(testCase["mutations"]);
    const cache = new Map<string, Json>();
    for (const sql of [String(testCase["sql"]), ...mutationSql(mutations)]) {
      cache.set(sql, (await serializeSql(sql, connection)) as Json);
    }
    const lookup = (sql: string): Json => {
      const document = cache.get(sql);
      assert.ok(document !== undefined, `no serialization prepared for ${sql}`);
      return document;
    };
    await assertGate(testCase, connection, (sql) => Promise.resolve(applyMutations(sql, mutations, lookup)));
  });
}

export async function runEngine(testCase: Case): Promise<void> {
  await withConnection(async (connection) => {
    for (const statement of strings(testCase["setup"])) {
      await connection.run(statement);
    }
    if (testCase["lockdown"] === true) {
      for (const statement of LOCKDOWN) {
        await connection.run(statement);
      }
    }
    for (const statement of strings(testCase["locked_setup"])) {
      await connection.run(statement);
    }
    const expect = record(testCase["expect"], "expect");
    const sql = String(testCase["sql"]);
    if ("error" in expect) {
      await assert.rejects(connection.runAndReadAll(sql), (error: unknown) => {
        assert.ok(error instanceof Error);
        if (expect["error"] === "permission") {
          assert.match(error.message, /^Permission Error:/u);
        }
        return true;
      });
      return;
    }
    const rows = (await connection.runAndReadAll(sql)).getRows();
    if (expect["nonempty"] === true) {
      assert.ok(rows.length > 0);
    }
    if ("rows" in expect) {
      const expected = list(expect["rows"]);
      assert.equal(rows.length, expected.length);
      expected.forEach((expectedRow, index) => {
        const row = rows[index] ?? [];
        const matchers = list(expectedRow);
        assert.equal(row.length, matchers.length);
        matchers.forEach((matcher, column) => {
          const value = row[column] ?? null;
          assert.ok(matchValue(matcher, value), `${String(value)} does not match ${JSON.stringify(matcher)}`);
        });
      });
    }
  });
}

function block(spec: Json): ContentBlock {
  const entry = record(spec, "content block");
  if (entry["type"] === "text") {
    return { type: "text", text: String(entry["text"]) };
  }
  return { type: String(entry["type"]), data: entry["data"], mimeType: entry["mimeType"] };
}

interface FixtureResult {
  readonly result: ToolResult;
  readonly structuredJson: string | null;
}

function fixtureResult(spec: JsonRecord): FixtureResult {
  const json = spec["structured_content_json"];
  const structuredJson = typeof json === "string" ? json : null;
  const result: ToolResult = {
    content: list(spec["content"]).map(block),
    ...(structuredJson === null ? {} : { structuredContent: JSON.parse(structuredJson) as Record<string, unknown> }),
    isError: spec["is_error"] === true,
  };
  return { result, structuredJson };
}

function flatReasonCategory(reason: unknown): string | null {
  if (reason === null || reason === undefined) {
    return null;
  }
  assert.equal(typeof reason, "string");
  return String(reason).split(/[:;]/u, 1)[0]?.trim() ?? "";
}

type Structured = Record<string, unknown>;

function compactColumn(column: unknown): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(column as Record<string, unknown>).filter(([, value]) => value !== null && value !== undefined),
  );
}

function assertHandle(expect: JsonRecord, result: ToolResult): Structured {
  const structured = result.structuredContent as Structured | undefined;
  assert.ok(structured !== undefined, "expected a handle");
  assert.notEqual(result.isError, true);
  const text = result.content[0]?.text;
  assert.equal(typeof text, "string");
  const body = String(text);
  const scopeId = String(structured["scope_id"]);
  assert.equal(structured["envelope_table"], scopeViewName(scopeId));
  assert.ok(body.includes(String(structured["envelope_table"])));
  for (const key of ["source_channel", "channel_conflict", "preview_complete", "scope_id"]) {
    if (key in expect) {
      assert.equal(structured[key], expect[key], key);
    }
  }
  if ("flat_reason" in expect) {
    assert.equal(flatReasonCategory(structured["flat_reason"]), expect["flat_reason"]);
  }
  const preview = String(structured["preview"]);
  if ("preview_equals" in expect) {
    assert.equal(preview, expect["preview_equals"]);
  }
  for (const fragment of strings(expect["preview_contains"])) {
    assert.ok(preview.includes(fragment), `preview lacks ${fragment}`);
  }
  for (const fragment of strings(expect["preview_excludes"])) {
    assert.ok(!preview.includes(fragment), `preview has ${fragment}`);
  }
  for (const fragment of strings(expect["text_contains"])) {
    assert.ok(body.includes(fragment), `text lacks ${fragment}:\n${body}`);
  }
  if ("text_max_bytes" in expect) {
    assert.ok(new TextEncoder().encode(body).length <= Number(expect["text_max_bytes"]));
  }
  if ("preview_rows" in expect || "preview_total_rows" in expect) {
    const found = /preview \(first ([0-9]+) of ([0-9]+) rows/u.exec(body);
    assert.ok(found !== null, body);
    if ("preview_rows" in expect) {
      assert.equal(Number(found[1]), expect["preview_rows"]);
    }
    if ("preview_total_rows" in expect) {
      assert.equal(Number(found[2]), expect["preview_total_rows"]);
    }
  }
  if ("tables" in expect) {
    const tables = structured["tables"] as Structured[];
    const wanted = list(expect["tables"]);
    assert.equal(tables.length, wanted.length);
    wanted.forEach((want, index) => {
      const table = tables[index] ?? {};
      const spec = record(want, "table");
      assert.ok(body.includes(String(table["name"])));
      if ("name_suffix" in spec) {
        assert.ok(String(table["name"]).endsWith(String(spec["name_suffix"])), String(table["name"]));
      }
      assert.equal(table["source_path"], spec["source_path"]);
      assert.equal(table["row_count"], spec["row_count"]);
      const columns = (table["columns"] as unknown[]).map(compactColumn);
      if ("columns" in spec) {
        assert.deepEqual(columns, list(spec["columns"]).map(compactColumn));
      }
      if ("column_count" in spec) {
        assert.equal(columns.length, spec["column_count"]);
      }
      for (const column of list(spec["columns_include"])) {
        assert.ok(
          columns.some((candidate) => JSON.stringify(candidate) === JSON.stringify(compactColumn(column))),
          `missing column ${JSON.stringify(column)}`,
        );
      }
    });
  }
  return structured;
}

function assertPassthrough(expect: JsonRecord, original: ToolResult, result: ToolResult): void {
  assert.deepEqual(result.structuredContent, original.structuredContent);
  assert.equal(result.isError, original.isError);
  assert.deepEqual(result.content.slice(0, original.content.length), original.content);
  const extra = result.content.slice(original.content.length);
  if (expect["passthrough"] === "oversize" || expect["size_note"] === true) {
    assert.equal(extra.length, 1);
    assert.equal(extra[0]?.type, "text");
  } else {
    assert.deepEqual(extra, []);
  }
}

class Session {
  readonly limits: Limits;
  readonly interceptor: Interceptor;
  readonly query: QueryTool;
  readonly calls = new Map<string, Structured>();

  constructor(limits: Limits, store: Store) {
    this.limits = limits;
    this.interceptor = new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined });
    this.query = new QueryTool(store, limits);
  }

  substitute(value: Json): Json {
    if (typeof value !== "string") {
      return value;
    }
    return value.replace(/\{\{([a-z0-9._-]+?)\.([a-z_]+)(?:\.([0-9]+))?\}\}/gu, (_, call: string, field: string, index?: string) => {
      const entry = this.calls.get(call);
      assert.ok(entry !== undefined, `no call ${call}`);
      switch (field) {
        case "table":
          return String((entry["tables"] as Structured[])[Number(index ?? 0)]?.["name"]);
        case "envelope":
          return scopeViewName(String(entry["scope_id"]));
        case "call_id":
          return String(entry["call_id"]);
        case "scope":
          return String(entry["scope_id"]);
        default:
          throw new Error(`unknown placeholder field ${field}`);
      }
    });
  }

  async call(step: JsonRecord): Promise<void> {
    const { result: original, structuredJson } = fixtureResult(record(step["result"], "result"));
    const server = typeof step["server"] === "string" ? step["server"] : "conformance";
    const tool = typeof step["tool"] === "string" ? step["tool"] : "tool";
    const meta = step["meta"] ?? null;
    const result = await this.interceptor.intercept({
      server,
      tool,
      mounted: mountedName(server, tool),
      arguments: step["arguments"] ?? null,
      result: original,
      ...(structuredJson === null ? {} : { structured: { json: structuredJson } }),
      meta,
      startedAt: new Date(),
    });
    const expect = record(step["expect"], "expect");
    const name = String(step["call"]);
    if (expect["outcome"] === "passthrough") {
      assertPassthrough(expect, original, result);
      const derived = derive(meta);
      this.calls.set(name, { tables: [], scope_id: derived.fromClient ? derived.tag : null });
      return;
    }
    this.calls.set(name, assertHandle(expect, result));
  }

  async runQuery(step: JsonRecord): Promise<void> {
    const args = Object.fromEntries(
      Object.entries(record(step["query"], "query")).map(([key, value]) => [key, this.substitute(value)]),
    );
    const result = await runQueryTool(this.query, args);
    const text = result.content[0]?.text ?? "";
    const expect = record(step["expect"], "expect");
    if (expect["error"] === true) {
      assert.equal(result.isError, true, text);
      assert.ok(!text.includes("row(s) shown"));
      assert.ok(!/^\|/mu.test(text));
      if ("categories" in expect) {
        assertRejection(expect, text);
      }
      return;
    }
    assert.notEqual(result.isError, true, text);
    assert.ok(new TextEncoder().encode(text).length <= this.limits.query_max_bytes);
    const view = parseQueryOutput(text);
    assert.equal(view.shown, view.rows.length);
    assertQueryView(expect, view);
    for (const fragment of strings(expect["text_excludes"])) {
      assert.ok(!text.includes(fragment));
    }
  }
}

export async function runSession(testCase: Case): Promise<void> {
  const limits = makeLimits(isRecord(testCase["limits"]) ? testCase["limits"] : {});
  const store = await Store.open(limits);
  try {
    const session = new Session(limits, store);
    for (const step of list(testCase["steps"])) {
      const entry = record(step, "step");
      if ("call" in entry) {
        await session.call(entry);
      } else {
        await session.runQuery(entry);
      }
    }
  } finally {
    store.close();
  }
}
