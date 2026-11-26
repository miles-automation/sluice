import { Server, type CallToolResult, type InputRequiredResult, type Tool } from "@modelcontextprotocol/server";
import { StdioServerTransport, serveStdio } from "@modelcontextprotocol/server/stdio";
import { dumpJson, type JsonValue } from "../../src/json.ts";
import { rawJson } from "../../src/wire.ts";

export const PAGE_TWO_TOOL = "page_two_only";
const PAGE_TWO_CURSOR = "p2";
const TAGS = ["alpha", "beta", "gamma"] as const;

type Args = Readonly<Record<string, unknown>> | undefined;
type Reply = CallToolResult | InputRequiredResult;

export interface Row {
  readonly id: number;
  readonly name: string;
  readonly score: number;
  readonly tag: string;
  readonly active: boolean;
}

export function rowsPayload(n: number): Row[] {
  return Array.from({ length: n }, (_, i) => ({
    id: i,
    name: `row-${String(i).padStart(4, "0")}`,
    score: Math.round(((i * 37) % 1000) / 7 * 1e4) / 1e4,
    tag: TAGS[i % TAGS.length] ?? "alpha",
    active: i % 2 === 0,
  }));
}

function rowValue(row: Row): JsonValue {
  return new Map<string, JsonValue>([
    ["id", BigInt(row.id)],
    ["name", row.name],
    ["score", row.score],
    ["tag", row.tag],
    ["active", row.active],
  ]);
}

function ok(payload: JsonValue): CallToolResult {
  return { content: [{ type: "text", text: dumpJson(payload) }] };
}

function intArg(args: Args, name: string, fallback: number): number {
  const value = args?.[name];
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && /^-?[0-9]+$/u.test(value)) {
    return Number(value);
  }
  return fallback;
}

function obj(entries: readonly (readonly [string, JsonValue])[]): JsonValue {
  return new Map(entries);
}

const OPEN_SCHEMA = { type: "object", properties: {}, additionalProperties: true } as const;
const PAGED_SCHEMA = {
  type: "object",
  properties: {
    n: { type: "integer" },
    limit: { type: "integer", minimum: 1, maximum: 200 },
    offset: { type: "integer", minimum: 0 },
  },
  required: ["limit"],
  additionalProperties: true,
} as const;

function tool(name: string, description: string, extra: Partial<Tool> = {}): Tool {
  return { name, description, inputSchema: { ...OPEN_SCHEMA }, ...extra };
}

function pagedTool(name: string, description: string): Tool {
  return {
    name,
    description,
    inputSchema: structuredClone(PAGED_SCHEMA) as unknown as Tool["inputSchema"],
    annotations: { readOnlyHint: true },
  };
}

export function fakeTools(): Tool[] {
  return [
    tool("rows", "n homogeneous objects under an 'items' key"),
    tool("bare_rows", "a bare JSON array of objects"),
    tool("just_text", "non-JSON prose"),
    tool("one_object", "a single JSON object"),
    tool("boom", "returns isError"),
    tool("picture", "returns an image content block"),
    tool("structured_only", "data in structuredContent, prose in content"),
    tool("both_channels", "both channels populated and disagreeing"),
    tool("lossless", "structuredContent with 2^63-1, 1.0 and values past 2^53"),
    tool("lossless_error", "isError with lossless structuredContent"),
    tool("echo_args", "echoes its arguments as structured content"),
    tool("rich_result", "carries result _meta and content annotations"),
    tool("client_capabilities", "reports the capabilities the connected client advertised"),
    tool("needs_input", "returns InputRequiredResult, completes on round two"),
    tool("slow", "sleeps delay_ms before answering"),
    tool("die", "exits the process"),
    tool("destructive", "carries annotations a client uses for confirmation", {
      annotations: { destructiveHint: true, title: "Destructive" },
      title: "Destructive Thing",
      _meta: { "vendor.example/owner": "fake" },
    }),
    tool("bad_schema", "declares an output schema and returns non-conforming output", {
      outputSchema: {
        type: "object",
        properties: { required_field: { type: "string" } },
        required: ["required_field"],
      },
    }),
    tool("hyphen-tool", "collides with hyphen_tool under naive slugging"),
    tool("hyphen_tool", "collides with hyphen-tool under naive slugging"),
    pagedTool("paged", "offset-paginated rows: limit/offset in, items/has_more/next_offset out"),
    pagedTool("paged_stuck", "has_more is true but next_offset never advances"),
    pagedTool("paged_no_next", "has_more is true but next_offset is null"),
    pagedTool("paged_error_at", "pages at offset >= 200 return isError"),
    pagedTool("paged_slow", "sleeps `delay_ms` per page"),
    pagedTool("paged_bad_items", "`items` is not an array"),
    pagedTool("paged_no_has_more", "the page omits has_more"),
    pagedTool("paged_interactive", "page 2 asks for input"),
    pagedTool("paged_lossless", "pages carrying 2^63-1 in structuredContent"),
    tool("paged_mutable", "paged, but without a read-only annotation"),
  ];
}

interface Page {
  items: JsonValue[];
  returned: number;
  hasMore: boolean;
  nextOffset: number | null;
  limit: number;
  offset: number;
}

function page(args: Args, n = 450): Page {
  const total = intArg(args, "n", n);
  const limit = intArg(args, "limit", 50);
  const offset = intArg(args, "offset", 0);
  const items = rowsPayload(total).slice(offset, offset + limit).map(rowValue);
  const hasMore = offset + items.length < total;
  return { items, returned: items.length, hasMore, nextOffset: hasMore ? offset + items.length : null, limit, offset };
}

function pageValue(value: Page, overrides: Readonly<Record<string, JsonValue | undefined>> = {}): JsonValue {
  const entries = new Map<string, JsonValue>([
    ["items", value.items],
    ["returned", BigInt(value.returned)],
    ["has_more", value.hasMore],
    ["next_offset", value.nextOffset === null ? null : BigInt(value.nextOffset)],
    ["limit", BigInt(value.limit)],
    ["offset", BigInt(value.offset)],
  ]);
  for (const [key, item] of Object.entries(overrides)) {
    if (item === undefined) {
      entries.delete(key);
    } else {
      entries.set(key, item);
    }
  }
  return entries;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const LOSSLESS_JSON = '{"items": [{"big": 9223372036854775807, "mid": 9007199254740993, "f": 1.0, "neg": -9223372036854775808}]}';

function raw(json: string): unknown {
  return JSON.parse(json, (_key, value: unknown, context?: { source?: string }) =>
    typeof value === "number" && context?.source !== undefined ? rawJson(context.source) : value,
  );
}

const ELICIT_SCHEMA = {
  type: "object",
  properties: { n: { type: "integer" } },
  required: ["n"],
} as const;

async function call(name: string, args: Args, state: string | undefined, responses: Readonly<Record<string, unknown>> | undefined, capabilities: unknown): Promise<Reply> {
  switch (name) {
    case "rows":
      return ok(obj([["items", rowsPayload(intArg(args, "n", 400)).map(rowValue)], ["next_cursor", null]]));
    case "bare_rows":
      return ok(rowsPayload(intArg(args, "n", 10)).map(rowValue));
    case "just_text":
      return { content: [{ type: "text", text: "no json here, only prose" }] };
    case "one_object":
      return ok(obj([["id", 1n], ["name", "solo"], ["nested", obj([["a", 1n]])]]));
    case "boom":
      return {
        content: [{ type: "text", text: "downstream says no" }],
        isError: true,
        _meta: { "vendor.example/errorCode": "E_NOPE" },
      };
    case "picture":
      return {
        content: [
          {
            type: "image",
            data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            mimeType: "image/png",
          },
        ],
        _meta: { "vendor.example/assetId": "img-7" },
      };
    case "structured_only":
      return {
        content: [{ type: "text", text: "Found 3 records for your query." }],
        structuredContent: { items: [0, 1, 2].map((i) => ({ id: i, score: i * 2 })) },
      };
    case "both_channels":
      return {
        content: [{ type: "text", text: dumpJson(obj([["scores", [100n, 200n]]])) }],
        structuredContent: { scores: [1, 2] },
      };
    case "lossless":
      return { content: [{ type: "text", text: "lossless numbers in structuredContent" }], structuredContent: raw(LOSSLESS_JSON) as Record<string, unknown> };
    case "lossless_error":
      return { content: [{ type: "text", text: "failed" }], structuredContent: raw(LOSSLESS_JSON) as Record<string, unknown>, isError: true };
    case "echo_args":
      return { content: [{ type: "text", text: "echo" }], structuredContent: { args: args ?? {} } };
    case "rich_result":
      return {
        content: [
          {
            type: "text",
            text: "not json",
            annotations: { audience: ["user"], priority: 0.7 },
            _meta: { "vendor.example/trace": "abc123" },
          },
        ],
        _meta: { "vendor.example/requestId": "req-42" },
      };
    case "client_capabilities": {
      const declared = (capabilities ?? {}) as Record<string, unknown>;
      return ok(obj([["sampling", declared["sampling"] !== undefined], ["elicitation", declared["elicitation"] !== undefined]]));
    }
    case "needs_input":
      if (state === undefined) {
        return {
          resultType: "input_required",
          inputRequests: {
            pick: { method: "elicitation/create", params: { mode: "form", message: "Pick a number", requestedSchema: ELICIT_SCHEMA } },
          },
          requestState: "awaiting-pick",
        } as unknown as InputRequiredResult;
      }
      return ok(
        obj([
          ["items", [obj([["round", 2n], ["state", state], ["answers", BigInt(Object.keys(responses ?? {}).length)], ["answer", JSON.stringify(responses ?? {})]])]],
        ]),
      );
    case "slow":
      await sleep(intArg(args, "delay_ms", 300));
      return ok(obj([["items", [obj([["done", true]])]]]));
    case "die":
      process.exit(3);
      break;
    case "destructive":
      return ok(obj([["items", [obj([["id", 1n]])]]]));
    case "bad_schema":
      return { content: [{ type: "text", text: "{}" }], structuredContent: { wrong_field: "nope" } };
    case "hyphen-tool":
    case "hyphen_tool":
      return ok(obj([["items", [obj([["which", name]])]]]));
    case PAGE_TWO_TOOL:
      return ok(obj([["items", [obj([["page", 2n]])]]]));
    case "paged":
    case "paged_mutable":
      return ok(pageValue(page(args)));
    case "paged_stuck":
      return ok(pageValue(page(args), { has_more: true, next_offset: 0n }));
    case "paged_no_next":
      return ok(pageValue(page(args), { has_more: true, next_offset: null }));
    case "paged_error_at":
      if (intArg(args, "offset", 0) >= 200) {
        return { content: [{ type: "text", text: "page unavailable" }], isError: true };
      }
      return ok(pageValue(page(args)));
    case "paged_slow":
      await sleep(intArg(args, "delay_ms", 300));
      return ok(pageValue(page(args)));
    case "paged_bad_items":
      return ok(pageValue(page(args), { items: "not a list" }));
    case "paged_no_has_more":
      return ok(pageValue(page(args), { has_more: undefined }));
    case "paged_interactive":
      if (intArg(args, "offset", 0) > 0 && state === undefined) {
        return {
          resultType: "input_required",
          inputRequests: {
            pick: { method: "elicitation/create", params: { mode: "form", message: "Continue?", requestedSchema: { type: "object", properties: {} } } },
          },
          requestState: "awaiting",
        } as unknown as InputRequiredResult;
      }
      return ok(pageValue(page(args)));
    case "paged_lossless": {
      const offset = intArg(args, "offset", 0);
      const more = offset === 0;
      const json = `{"items": [{"big": 9223372036854775807, "f": 1.0, "at": ${offset}}], "has_more": ${more}, "next_offset": ${more ? 1 : "null"}}`;
      return { content: [{ type: "text", text: "page" }], structuredContent: raw(json) as Record<string, unknown> };
    }
    default:
      return { content: [{ type: "text", text: `unknown tool ${name}` }], isError: true };
  }
}

export function buildFakeServer(): Server {
  const server = new Server({ name: "fake-downstream", version: "0.1.0" }, { capabilities: { tools: { listChanged: false } } });
  server.setRequestHandler("tools/list", (request) => {
    if (request.params?.cursor === PAGE_TWO_CURSOR) {
      return { tools: [tool(PAGE_TWO_TOOL, "reachable only by following next_cursor")] };
    }
    return { tools: fakeTools(), nextCursor: PAGE_TWO_CURSOR };
  });
  server.setRequestHandler("tools/call", async (request, ctx) => {
    const state = ctx.mcpReq.requestState();
    const declared = ctx.mcpReq.envelope?.["io.modelcontextprotocol/clientCapabilities" as keyof typeof ctx.mcpReq.envelope] ?? server.getClientCapabilities();
    return call(
      request.params.name,
      request.params.arguments,
      typeof state === "string" ? state : undefined,
      ctx.mcpReq.inputResponses,
      declared,
    );
  });
  return server;
}

if (import.meta.main) {
  if (process.argv.includes("--legacy")) {
    await buildFakeServer().connect(new StdioServerTransport());
  } else {
    serveStdio(buildFakeServer);
  }
}
