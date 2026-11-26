import { Server, type CallToolResult, type InputRequiredResult, type Tool } from "@modelcontextprotocol/server";
import { DownstreamError, FailureClass, errorResult } from "./errors.ts";
import { paginationObject } from "./handle.ts";
import type { Interceptor } from "./intercept.ts";
import { isJsonObject, type JsonObject, type JsonValue } from "./json.ts";
import { logger } from "./log.ts";
import type { ContentBlock, PaginationSummary, ToolResult } from "./models.ts";
import { PaginationArgumentError, structuredOf, type DownstreamReply, type FetchOutcome, type FinalResult } from "./paginate.ts";
import { fromPlain } from "./payload.ts";
import {
  CLIENT_CAPABILITIES_KEY,
  OLDEST_MODERN_VERSION,
  argumentsOf,
  isModern,
  type MountedCollection,
  type Proxy,
} from "./proxy.ts";
import { QUERY_DESCRIPTION, QUERY_SCHEMA, QUERY_TOOL_NAME, runQueryTool, type QueryTool } from "./query.ts";
import { VERSION } from "./version.ts";
import { toWire } from "./wire.ts";

const log = logger("sluice.server");

export const SERVER_NAME = "sluice";

export const INSTRUCTIONS =
  "Tool results are materialized into a scratch database. Each call returns a " +
  "short preview plus a table name; run SQL over the full result with `query`.";


export type RequestLookup = (id: string | number) => JsonObject | undefined;

export interface ServerParts {
  readonly proxy: Proxy;
  readonly interceptor?: Interceptor | null;
  readonly query?: QueryTool | null;
  readonly requests?: RequestLookup;
}

export interface CollectionSource {
  fetchCollection(
    entry: MountedCollection,
    args: JsonObject | null,
    capabilities?: JsonValue,
    cancel?: AbortSignal,
  ): Promise<FetchOutcome>;
}

export interface CallContext {
  readonly arguments: JsonObject | null;
  readonly capabilities?: JsonValue | undefined;
  readonly signal?: AbortSignal | undefined;
  readonly meta: unknown;
  readonly startedAt: Date;
}

type Reply = CallToolResult | InputRequiredResult;

export function queryToolDefinition(): Tool {
  return {
    name: QUERY_TOOL_NAME,
    title: "Query materialized results",
    description: QUERY_DESCRIPTION,
    inputSchema: structuredClone(QUERY_SCHEMA) as unknown as Tool["inputSchema"],
    annotations: { readOnlyHint: true, destructiveHint: false },
  };
}

function textError(text: string): ToolResult {
  return { content: [{ type: "text", text: `sluice: ${text}` }], isError: true };
}

export function summarize(outcome: FetchOutcome): PaginationSummary {
  return {
    status: outcome.status,
    reason: outcome.reason,
    detail: outcome.detail,
    pages: outcome.pages,
    rows: outcome.rows.length,
    bytes: outcome.bytes,
    seconds: outcome.seconds,
    resumeOffset: outcome.resumeOffset,
  };
}

function relay(returned: ToolResult, original: ToolResult, structured: JsonValue | null | undefined): ToolResult {
  if (structured === undefined || structured === null || returned.structuredContent !== original.structuredContent) {
    return returned;
  }
  return { ...returned, structuredContent: toWire(structured) as Record<string, unknown> };
}

function textsOf(result: ToolResult): string[] {
  return result.content.flatMap((block) => (block.type === "text" && typeof block.text === "string" ? [block.text] : []));
}

export function collectionResult(recorded: ToolResult, summary: PaginationSummary): ToolResult {
  const structured = recorded.structuredContent;
  if (typeof structured === "object" && structured !== null && !Array.isArray(structured) && "pagination" in structured) {
    return recorded;
  }
  const text = "sluice: the collection was fetched but could not be materialized; no rows are returned.";
  return {
    content: [{ type: "text", text: [text, ...textsOf(recorded)].join("\n") }],
    structuredContent: { pagination: paginationObject(summary), tables: [] },
    isError: true,
  };
}

async function collect(
  source: CollectionSource,
  interceptor: Interceptor | null,
  collection: MountedCollection,
  context: CallContext,
): Promise<ToolResult> {
  let outcome: FetchOutcome;
  try {
    outcome = await source.fetchCollection(collection, context.arguments, context.capabilities, context.signal);
  } catch (error) {
    if (error instanceof PaginationArgumentError) {
      return textError(error.message);
    }
    throw error;
  }
  const summary = summarize(outcome);
  const recordedArgs = new Map(context.arguments ?? []);
  recordedArgs.set("sluice_pagination", fromPlain(paginationObject(summary)));

  if (outcome.pages === 0) {
    const note: ContentBlock = {
      type: "text",
      text: `sluice: pagination fetched no pages (${summary.reason}): ${String(summary.detail)}`,
    };
    const failed = outcome.firstPageError;
    const result: ToolResult =
      failed === null
        ? { content: [note], isError: true }
        : { ...failed.result, content: [...failed.result.content, note] };
    if (interceptor === null) {
      return result;
    }
    const recorded = await interceptor.intercept({
      server: collection.server,
      tool: collection.original.name,
      mounted: collection.mounted,
      arguments: recordedArgs,
      result,
      ...(failed === null ? {} : { structured: { value: structuredOf(failed) } }),
      meta: context.meta,
      startedAt: context.startedAt,
      failureClass: summary.reason,
      admitted: true,
    });
    return failed === null ? recorded : relay(recorded, result, failed.structured);
  }

  const items = new Map<string, JsonValue>([["items", outcome.rows]]);
  const combined: ToolResult = { content: [] };
  if (interceptor === null) {
    return { content: [], structuredContent: toWire(items) as Record<string, unknown> };
  }
  const recorded = await interceptor.intercept({
    server: collection.server,
    tool: collection.original.name,
    mounted: collection.mounted,
    arguments: recordedArgs,
    result: combined,
    structured: { value: items },
    meta: context.meta,
    startedAt: context.startedAt,
    payloadCeiling: 2 * collection.config.max_bytes,
    pagination: summary,
    admitted: true,
  });
  return collectionResult(recorded, summary);
}

export function runCollection(
  source: CollectionSource,
  interceptor: Interceptor | null,
  collection: MountedCollection,
  context: CallContext,
): Promise<ToolResult> {
  if (interceptor === null) {
    return collect(source, null, collection, context);
  }
  return interceptor.withAdmission(() => collect(source, interceptor, collection, context));
}

async function runQuery(query: QueryTool, args: unknown): Promise<ToolResult> {
  const result = await runQueryTool(query, args);
  if (result.isError !== true) {
    return { content: result.content };
  }
  return { isError: true, content: result.content.map((block) => ({ ...block, text: `sluice: ${block.text}` })) };
}

interface RawRequest {
  readonly arguments: JsonObject | null;
  readonly inputResponses: JsonValue | undefined;
  readonly requestState: string | undefined;
  readonly capabilities: JsonValue | undefined;
}

function rawRequest(
  raw: JsonObject | undefined,
  params: { readonly arguments?: Record<string, unknown> | undefined },
  inputResponses: Record<string, unknown> | undefined,
  requestState: unknown,
): RawRequest {
  if (raw !== undefined) {
    const state = raw.get("requestState");
    const meta = raw.get("_meta");
    return {
      arguments: argumentsOf(raw.get("arguments")),
      inputResponses: raw.get("inputResponses"),
      requestState: typeof state === "string" ? state : undefined,
      capabilities: isJsonObject(meta) ? meta.get(CLIENT_CAPABILITIES_KEY) : undefined,
    };
  }
  const args = params.arguments === undefined ? null : fromPlain(params.arguments);
  return {
    arguments: argumentsOf(args),
    inputResponses: inputResponses === undefined ? undefined : fromPlain(inputResponses),
    requestState: typeof requestState === "string" ? requestState : undefined,
    capabilities: undefined,
  };
}

function legacyCapabilities(server: Server): JsonValue | undefined {
  const declared = server.getClientCapabilities();
  return declared === undefined ? undefined : fromPlain(declared);
}

export function buildServer(parts: ServerParts): Server {
  const { proxy } = parts;
  const interceptor = parts.interceptor ?? null;
  const query = parts.query ?? null;
  const server = new Server(
    { name: SERVER_NAME, version: VERSION },
    {
      capabilities: { tools: { listChanged: false } },
      instructions: INSTRUCTIONS,
      inputRequired: { legacyShim: false },
    },
  );

  server.setRequestHandler("tools/list", () => {
    const tools = proxy.mountedTools();
    return { tools: query === null ? tools : [...tools, queryToolDefinition()] };
  });

  server.setRequestHandler("tools/call", async (request, ctx): Promise<Reply> => {
    const { name } = request.params;
    if (name === QUERY_TOOL_NAME) {
      if (query === null) {
        return textError("the query tool is not available") as CallToolResult;
      }
      return (await runQuery(query, request.params.arguments)) as CallToolResult;
    }
    const startedAt = new Date();
    const id = ctx.mcpReq.id;
    const raw = parts.requests?.(typeof id === "string" || typeof id === "number" ? id : String(id));
    const call = rawRequest(raw, request.params, ctx.mcpReq.inputResponses, ctx.mcpReq.requestState());
    const meta: unknown = ctx.mcpReq._meta ?? request.params._meta ?? null;
    const upstreamModern = isModern(server.getNegotiatedProtocolVersion());
    const capabilities = upstreamModern ? call.capabilities : legacyCapabilities(server);
    const context: CallContext = { arguments: call.arguments, capabilities, signal: ctx.mcpReq.signal, meta, startedAt };

    const collection = proxy.resolveCollection(name);
    if (collection !== undefined) {
      return (await runCollection(proxy, interceptor, collection, context)) as CallToolResult;
    }

    const recordFailure = async (failure: DownstreamError): Promise<Reply> => {
      const result = errorResult(failure);
      if (interceptor === null) {
        return result as CallToolResult;
      }
      const entry = proxy.resolve(name);
      return (await interceptor.intercept({
        server: entry?.server ?? "unknown",
        tool: failure.tool,
        mounted: name,
        arguments: call.arguments,
        result,
        meta,
        startedAt,
        failureClass: failure.failureClass,
      })) as CallToolResult;
    };

    let reply: DownstreamReply;
    try {
      reply = await proxy.call(
        name,
        {
          arguments: call.arguments,
          inputResponses: call.inputResponses,
          requestState: call.requestState,
          capabilities,
        },
        ctx.mcpReq.signal,
      );
    } catch (error) {
      if (error instanceof DownstreamError) {
        log.warning(`downstream failure: ${error.message}`);
        return recordFailure(error);
      }
      throw error;
    }
    if (reply.kind === "input_required") {
      if (!upstreamModern) {
        return recordFailure(
          new DownstreamError(
            FailureClass.PROTOCOL,
            name,
            `the tool requested input, which requires protocol ${OLDEST_MODERN_VERSION}; this client negotiated ${String(server.getNegotiatedProtocolVersion())}`,
          ),
        );
      }
      return reply.result as unknown as InputRequiredResult;
    }
    return (await finalReply(parts, name, reply, context)) as CallToolResult;
  });

  return server;
}

async function finalReply(parts: ServerParts, name: string, reply: FinalResult, context: CallContext): Promise<ToolResult> {
  const interceptor = parts.interceptor ?? null;
  if (interceptor === null) {
    return relay(reply.result, reply.result, reply.structured);
  }
  const entry = parts.proxy.resolve(name);
  const recorded = await interceptor.intercept({
    server: entry?.server ?? "unknown",
    tool: entry?.original.name ?? name,
    mounted: name,
    arguments: context.arguments,
    result: reply.result,
    ...(reply.structured === undefined ? {} : { structured: { value: reply.structured } }),
    meta: context.meta,
    startedAt: context.startedAt,
  });
  return relay(recorded, reply.result, reply.structured);
}
