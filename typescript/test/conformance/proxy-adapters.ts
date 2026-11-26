import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Tool } from "@modelcontextprotocol/client";
import { CONFORMANCE_ROOT, type Case, type Json } from "./fixtures.ts";
import { ConfigError, loadConfig, paginationConfig, type PaginationConfig } from "../../src/config.ts";
import { DownstreamError, FailureClass } from "../../src/errors.ts";
import { Interceptor } from "../../src/intercept.ts";
import { isJsonObject, parseJson, type JsonObject, type JsonValue } from "../../src/json.ts";
import { makeLimits } from "../../src/limits.ts";
import { fetchAll, PaginationArgumentError, type DownstreamReply, type PageCall } from "../../src/paginate.ts";
import { exposeCollection } from "../../src/proxy.ts";
import { runCollection } from "../../src/server.ts";
import { Store } from "../../src/store.ts";

type JsonRecord = { readonly [key: string]: Json };

function isRecord(value: Json | undefined): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: Json | undefined): JsonRecord {
  return isRecord(value) ? value : {};
}

function strings(value: Json | undefined): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function plain(value: JsonValue): Json {
  if (typeof value === "bigint") {
    return Number(value);
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(plain);
  }
  const out: { [key: string]: Json } = {};
  for (const [key, item] of value as JsonObject) {
    out[key] = plain(item);
  }
  return out;
}

function lossless(value: Json | undefined): JsonObject | null {
  if (value === undefined || value === null) {
    return null;
  }
  const parsed = parseJson(JSON.stringify(value));
  assert.ok(isJsonObject(parsed), "arguments must be an object");
  return parsed;
}

const REPO_ROOT = fileURLToPath(new URL("../", CONFORMANCE_ROOT));

export function runConfig(testCase: Case): void {
  const directory = mkdtempSync(join(tmpdir(), "sluice-config-"));
  try {
    let path = join(directory, "sluice.toml");
    if (typeof testCase["toml_file"] === "string") {
      path = join(REPO_ROOT, testCase["toml_file"]);
    } else if (typeof testCase["toml"] === "string") {
      writeFileSync(path, testCase["toml"], "utf8");
    }
    const environ = Object.fromEntries(Object.entries(record(testCase["environ"])).map(([key, value]) => [key, String(value)]));
    const expect = record(testCase["expect"]);
    if (expect["error"] === true) {
      assert.throws(
        () => loadConfig(path, environ),
        (error: unknown) => {
          assert.ok(error instanceof ConfigError, `expected a ConfigError, got ${String(error)}`);
          for (const fragment of strings(expect["mentions"])) {
            assert.ok(error.message.includes(fragment), `${fragment} not named in ${error.message}`);
          }
          return true;
        },
      );
      return;
    }
    const config = loadConfig(path, environ);
    for (const [key, value] of Object.entries(record(expect["server"]))) {
      assert.deepEqual((config.server as unknown as Record<string, unknown>)[key], value, key);
    }
    for (const [key, value] of Object.entries(record(expect["limits"]))) {
      assert.equal((config.limits as unknown as Record<string, unknown>)[key], value, key);
    }
    if ("pagination" in expect) {
      const wanted = record(expect["pagination"]);
      assert.deepEqual([...config.pagination.keys()].sort(), Object.keys(wanted).sort());
      for (const [tool, fields] of Object.entries(wanted)) {
        const entry = config.pagination.get(tool) as unknown as Record<string, unknown>;
        for (const [key, value] of Object.entries(record(fields))) {
          assert.equal(entry[key], value, `${tool}.${key}`);
        }
      }
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

const FAILURES: Readonly<Record<string, FailureClass>> = {
  output_schema: FailureClass.OUTPUT_SCHEMA,
  protocol: FailureClass.PROTOCOL,
  transport: FailureClass.TRANSPORT,
};

function scripted(testCase: Case, config: PaginationConfig, seen: JsonObject[]): PageCall {
  const pages = record(testCase["pages"]);
  return (args) => {
    seen.push(new Map(args));
    const offset = args.get(config.offset_arg);
    const response = record(pages[String(offset)]);
    if (typeof response["failure"] === "string") {
      const failure = FAILURES[response["failure"]] ?? FailureClass.PROTOCOL;
      return Promise.reject(new DownstreamError(failure, "t", String(response["message"])));
    }
    if (response["input_required"] === true) {
      const reply: DownstreamReply = {
        kind: "input_required",
        result: {
          resultType: "input_required",
          inputRequests: {
            pick: { method: "elicitation/create", params: { message: "Continue?", requestedSchema: { type: "object", properties: {} } } },
          },
          requestState: "awaiting",
        },
      };
      return Promise.resolve(reply);
    }
    return Promise.resolve({
      kind: "result",
      result: { content: [{ type: "text", text: String(response["text"]) }], isError: response["is_error"] === true },
    });
  };
}

export async function runPaginate(testCase: Case): Promise<void> {
  const config = paginationConfig("t", record(testCase["config"]));
  const seen: JsonObject[] = [];
  const call = scripted(testCase, config, seen);
  const args = lossless(testCase["arguments"]);
  const expect = record(testCase["expect"]);
  if (expect["argument_error"] === true) {
    await assert.rejects(fetchAll(call, config, args), (error: unknown) => {
      assert.ok(error instanceof PaginationArgumentError);
      for (const fragment of strings(expect["mentions"])) {
        assert.ok(error.message.includes(fragment), `${fragment} not in ${error.message}`);
      }
      return true;
    });
    assert.deepEqual(seen, []);
  } else {
    const outcome = await fetchAll(call, config, args);
    assert.equal(outcome.status, expect["status"]);
    assert.equal(outcome.reason, expect["reason"]);
    const observed: Record<string, Json> = {
      pages: outcome.pages,
      bytes: outcome.bytes,
      resume_offset: outcome.resumeOffset,
      rows: outcome.rows.map(plain),
    };
    for (const key of ["pages", "bytes", "resume_offset", "rows"]) {
      if (key in expect) {
        assert.deepEqual(observed[key], expect[key], key);
      }
    }
    if ("calls" in expect) {
      assert.deepEqual(seen.map(plain), expect["calls"]);
    }
    for (const fragment of strings(expect["detail_contains"])) {
      assert.ok(outcome.detail?.includes(fragment), `${fragment} not in ${String(outcome.detail)}`);
    }
    if ("first_page_error" in expect) {
      assert.equal(outcome.firstPageError !== null, expect["first_page_error"]);
    }
  }
  if ("collection" in testCase) {
    seen.length = 0;
    await assertCollection(testCase, config, call, args);
  }
}

async function assertCollection(testCase: Case, config: PaginationConfig, call: PageCall, args: JsonObject | null): Promise<void> {
  const expect = record(testCase["collection"]);
  const tool: Tool = { name: "t", inputSchema: { type: "object", properties: {} }, annotations: { readOnlyHint: true } };
  const entry = exposeCollection("conformance", tool, config);
  const limits = makeLimits();
  const store = await Store.open(limits);
  let result;
  let tables: string[];
  try {
    const interceptor = new Interceptor(store, limits, { queryAvailable: true, logger: () => undefined });
    result = await runCollection(
      { fetchCollection: (mounted, fetchArgs) => fetchAll(call, mounted.config, fetchArgs) },
      interceptor,
      entry,
      { arguments: args, meta: null, startedAt: new Date() },
    );
    tables = [...store.allowedObjects].filter((name) => !name.startsWith("sluice_calls__"));
  } finally {
    store.close();
  }
  const structured = result.structuredContent as Record<string, unknown> | undefined;
  if (expect["outcome"] === "error") {
    assert.equal(result.isError, true);
    assert.ok(structured === undefined || JSON.stringify(structured["tables"]) === "[]");
    assert.deepEqual(tables, [], "a fetch with no rows must not leave a table");
    const texts = result.content.flatMap((block) => (typeof block.text === "string" ? [block.text] : []));
    assert.ok(texts.length > 0);
    if ("keeps_downstream_error" in expect) {
      const first = record(record(testCase["pages"])["0"]);
      const kept = texts[0] === first["text"] && texts.length >= 2;
      assert.equal(kept, expect["keeps_downstream_error"]);
    }
    return;
  }
  assert.notEqual(result.isError, true);
  assert.ok(structured !== undefined);
  const listed = structured["tables"] as readonly Record<string, unknown>[];
  if (expect["outcome"] === "envelope_only") {
    assert.deepEqual(listed, []);
    assert.deepEqual(tables, []);
  } else {
    assert.equal(listed.length, 1);
    assert.equal(tables.length, 1);
    if ("table_rows" in expect) {
      assert.equal(listed[0]?.["row_count"], expect["table_rows"]);
    }
  }
  const pagination = structured["pagination"] as Record<string, unknown>;
  for (const key of ["status", "reason", "resume_offset"]) {
    if (key in expect) {
      assert.equal(pagination[key], expect[key], key);
    }
  }
}
