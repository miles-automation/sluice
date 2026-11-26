import { Client, ProtocolError, SdkError, SdkErrorCode, type Tool } from "@modelcontextprotocol/client";
import type { Config, PaginationConfig, ServerConfig } from "./config.ts";
import { ConfigError } from "./config.ts";
import { DownstreamError, FailureClass } from "./errors.ts";
import { isJsonObject, type JsonObject, type JsonValue } from "./json.ts";
import { logger } from "./log.ts";
import type { ToolResult } from "./models.ts";
import { NameCollisionError, assertInjective, collectionName, mountedName } from "./naming.ts";
import { fetchAll, type DownstreamReply, type FetchOutcome } from "./paginate.ts";
import { DownstreamTransport, capturing } from "./transport.ts";
import { VERSION } from "./version.ts";
import { toPlainObject, toWire } from "./wire.ts";

const log = logger("sluice.proxy");

export const HANDLE_NOTE =
  "Results from this tool are stored in a session database. You receive a preview " +
  "plus a table name; use the `query` tool to run SQL over the full result.";

export function collectionNote(tool: string, limit: string, offset: string): string {
  return (
    `Fetches every page of \`${tool}\` (up to configured limits) and records ALL rows in ONE ` +
    `table. Pass the tool's filters only: Sluice supplies \`${limit}\` and \`${offset}\`. An ` +
    `\`${offset}\` may be given to resume a partial fetch. The handle states whether the fetch ` +
    "was complete or partial and why it stopped."
  );
}

export function collectionCrossReference(collection: string): string {
  return `To fetch every page of this tool at once into one table, call \`${collection}\` instead.`;
}

export const MAX_LIST_PAGES = 1000;
export const MODERN_VERSIONS: ReadonlySet<string> = new Set(["2026-07-28"]);
export const OLDEST_MODERN_VERSION = "2026-07-28";
export const CALL_TIMEOUT_MS = 2_147_483_647;
export const PROBE_TIMEOUT_MS = 15_000;
export const CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities";

const OUTPUT_SCHEMA_MARKERS = ["output schema", "structured content"] as const;
const TRANSPORT_CODES: ReadonlySet<string> = new Set([
  SdkErrorCode.ConnectionClosed,
  SdkErrorCode.NotConnected,
  SdkErrorCode.SendFailed,
]);

export interface MountedTool {
  readonly mounted: string;
  readonly server: string;
  readonly original: Tool;
  readonly exposed: Tool;
}

export interface MountedCollection extends MountedTool {
  readonly config: PaginationConfig;
}

export interface CallParams {
  readonly name: string;
  readonly arguments: JsonObject | null;
  readonly inputResponses?: JsonValue | undefined;
  readonly requestState?: string | undefined;
  readonly capabilities?: JsonValue | undefined;
}

export interface CallOptions {
  readonly toolDefinition: Tool;
  readonly signal?: AbortSignal | undefined;
}

export interface ListedPage {
  readonly tools: readonly Tool[];
  readonly nextCursor?: string | undefined;
}

export interface Session {
  readonly protocolVersion: string | undefined;
  listTools(cursor: string | undefined): Promise<ListedPage>;
  callTool(params: CallParams, options: CallOptions): Promise<DownstreamReply>;
  close(): Promise<void>;
}

function withoutOutputSchema(tool: Tool, update: Partial<Tool>): Tool {
  const exposed: Record<string, unknown> = { ...structuredClone(tool), ...update };
  delete exposed["outputSchema"];
  return exposed as unknown as Tool;
}

export function expose(server: string, tool: Tool): MountedTool {
  const mounted = mountedName(server, tool.name);
  const description = `${tool.description ?? ""}\n\n${HANDLE_NOTE}`.trim();
  return { mounted, server, original: tool, exposed: withoutOutputSchema(tool, { name: mounted, description }) };
}

export function exposeCollection(server: string, tool: Tool, config: PaginationConfig): MountedCollection {
  const mounted = collectionName(server, tool.name);
  const source: unknown = tool.inputSchema;
  const schema: Record<string, unknown> =
    typeof source === "object" && source !== null && !Array.isArray(source)
      ? (structuredClone(source) as Record<string, unknown>)
      : { type: "object", properties: {} };
  const properties = schema["properties"];
  if (typeof properties === "object" && properties !== null && !Array.isArray(properties)) {
    delete (properties as Record<string, unknown>)[config.limit_arg];
  }
  const required = schema["required"];
  if (Array.isArray(required)) {
    schema["required"] = required.filter((name) => name !== config.limit_arg);
  }
  const note = collectionNote(tool.name, config.limit_arg, config.offset_arg);
  const description = `${tool.description ?? ""}\n\n${note}\n\n${HANDLE_NOTE}`.trim();
  const exposed = withoutOutputSchema(tool, {
    name: mounted,
    description,
    inputSchema: schema as Tool["inputSchema"],
  });
  return { mounted, server, original: tool, exposed, config };
}

function readOnly(tool: Tool): boolean {
  return tool.annotations?.readOnlyHint === true;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isModern(version: string | undefined): boolean {
  return version !== undefined && MODERN_VERSIONS.has(version);
}

class SdkSession implements Session {
  readonly #client: Client;

  private constructor(client: Client) {
    this.#client = client;
  }

  static async open(server: ServerConfig, onclose: () => void): Promise<SdkSession> {
    const attempt = async (mode: "auto" | "legacy"): Promise<SdkSession> => {
      const transport = new DownstreamTransport({
        command: server.command,
        args: server.args,
        env: server.env,
        cwd: server.cwd,
      });
      const client = new Client(
        { name: "sluice", version: VERSION },
        {
          capabilities: {},
          versionNegotiation: { mode, probe: { timeoutMs: PROBE_TIMEOUT_MS } },
          inputRequired: { autoFulfill: false },
        },
      );
      client.onerror = (error) => log.debug(`downstream: ${error.message}`);
      try {
        await client.connect(transport);
      } catch (error) {
        await client.close().catch(() => undefined);
        await transport.close().catch(() => undefined);
        throw error;
      }
      client.onclose = onclose;
      return new SdkSession(client);
    };
    try {
      return await attempt("auto");
    } catch (error) {
      if (typeof (error as { code?: unknown }).code === "string" && (error as { syscall?: unknown }).syscall !== undefined) {
        throw new DownstreamError(FailureClass.TRANSPORT, server.name, `could not start downstream: ${message(error)}`);
      }
      log.debug(`version negotiation failed (${message(error)}); retrying with the initialize handshake`);
    }
    try {
      return await attempt("legacy");
    } catch (error) {
      throw new DownstreamError(FailureClass.TRANSPORT, server.name, `could not start downstream: ${message(error)}`);
    }
  }

  get protocolVersion(): string | undefined {
    return this.#client.getNegotiatedProtocolVersion();
  }

  async listTools(cursor: string | undefined): Promise<ListedPage> {
    const result = await this.#client.request(
      { method: "tools/list", params: cursor === undefined ? {} : { cursor } },
      { timeout: CALL_TIMEOUT_MS },
    );
    return { tools: result.tools, nextCursor: result.nextCursor };
  }

  async callTool(params: CallParams, options: CallOptions): Promise<DownstreamReply> {
    const wire: Record<string, unknown> = { name: params.name };
    if (params.arguments !== null) {
      wire["arguments"] = toPlainObject(params.arguments);
    }
    if (params.inputResponses !== undefined) {
      wire["inputResponses"] = toWire(params.inputResponses);
    }
    if (params.requestState !== undefined) {
      wire["requestState"] = params.requestState;
    }
    if (params.capabilities !== undefined && this.protocolVersion !== undefined && isModern(this.protocolVersion)) {
      wire["_meta"] = { [CLIENT_CAPABILITIES_KEY]: toWire(params.capabilities) };
    }
    return capturing(async (capture) => {
      const result: unknown = await this.#client.callTool(wire as Parameters<Client["callTool"]>[0], {
        allowInputRequired: true,
        toolDefinition: options.toolDefinition,
        timeout: CALL_TIMEOUT_MS,
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      });
      const record = result as Record<string, unknown>;
      if (record["resultType"] === "input_required") {
        return { kind: "input_required", result: record };
      }
      const raw = capture.raw;
      if (raw === undefined) {
        return { kind: "result", result: record as unknown as ToolResult };
      }
      const structured = raw.get("structuredContent");
      return { kind: "result", result: record as unknown as ToolResult, structured: structured ?? null };
    });
  }

  close(): Promise<void> {
    return this.#client.close();
  }
}

export interface ProxyOptions {
  readonly mount?: (server: string, tool: string) => string;
}

export class Proxy {
  readonly #config: Config;
  readonly #session: Session;
  readonly #mount: (server: string, tool: string) => string;
  #tools = new Map<string, MountedTool>();
  #collections = new Map<string, MountedCollection>();
  #healthy = true;

  constructor(config: Config, session: Session, options: ProxyOptions = {}) {
    this.#config = config;
    this.#session = session;
    this.#mount = options.mount ?? mountedName;
  }

  static async start(config: Config, options: ProxyOptions = {}): Promise<Proxy> {
    let proxy: Proxy | undefined;
    const session = await SdkSession.open(config.server, () => proxy?.markUnhealthy());
    proxy = new Proxy(config, session, options);
    try {
      await proxy.refreshTools();
    } catch (error) {
      await session.close().catch(() => undefined);
      throw error;
    }
    if (!isModern(session.protocolVersion)) {
      log.warning(
        `downstream negotiated ${String(session.protocolVersion)}; interactive tool calls are unavailable below ${OLDEST_MODERN_VERSION}`,
      );
    }
    return proxy;
  }

  get session(): Session {
    return this.#session;
  }

  get protocolVersion(): string | undefined {
    return this.#session.protocolVersion;
  }

  get supportsRoundTrips(): boolean {
    return isModern(this.#session.protocolVersion);
  }

  get healthy(): boolean {
    return this.#healthy;
  }

  markUnhealthy(): void {
    this.#healthy = false;
  }

  close(): Promise<void> {
    return this.#session.close();
  }

  async refreshTools(): Promise<void> {
    const tools = await this.#listAllTools();
    const server = this.#config.server.name;
    assertInjective(
      tools.map((tool) => [server, tool.name] as const),
      this.#mount,
    );
    const mounted = new Map<string, MountedTool>();
    for (const tool of tools) {
      const entry = expose(server, tool);
      mounted.set(entry.mounted, entry);
    }
    this.#collections = this.#mountCollections(server, tools, mounted);
    this.#tools = mounted;
    log.info(`mounted ${mounted.size} downstream tools and ${this.#collections.size} collections`);
  }

  #mountCollections(server: string, tools: readonly Tool[], mounted: Map<string, MountedTool>): Map<string, MountedCollection> {
    const byName = new Map(tools.map((tool) => [tool.name, tool]));
    const collections = new Map<string, MountedCollection>();
    for (const [toolName, pagination] of this.#config.pagination) {
      const original = byName.get(toolName);
      if (original === undefined) {
        throw new ConfigError(`[pagination.${toolName}] names a tool the downstream did not list`);
      }
      if (!readOnly(original)) {
        throw new ConfigError(
          `[pagination.${toolName}] refused: the downstream tool is not annotated ` +
            "readOnlyHint=true, and Sluice only fetches read-only tools automatically",
        );
      }
      const entry = exposeCollection(server, original, pagination);
      if (mounted.has(entry.mounted) || collections.has(entry.mounted)) {
        throw new NameCollisionError(
          `collection for ${server}/${JSON.stringify(toolName)} collides with an existing mounted name ${JSON.stringify(entry.mounted)}`,
        );
      }
      collections.set(entry.mounted, entry);
      const singleName = mountedName(server, toolName);
      const single = mounted.get(singleName);
      if (single !== undefined) {
        mounted.set(singleName, {
          ...single,
          exposed: { ...single.exposed, description: `${single.exposed.description ?? ""}\n\n${collectionCrossReference(entry.mounted)}` },
        });
      }
    }
    return collections;
  }

  async #listAllTools(): Promise<Tool[]> {
    const collected: Tool[] = [];
    const seen = new Set<string>();
    let cursor: string | undefined;
    for (let page = 0; page < MAX_LIST_PAGES; page += 1) {
      let result: ListedPage;
      try {
        result = await this.#session.listTools(cursor);
      } catch (error) {
        throw new DownstreamError(FailureClass.PROTOCOL, this.#config.server.name, `could not list tools: ${message(error)}`);
      }
      collected.push(...result.tools);
      const next = result.nextCursor;
      if (next === undefined) {
        return collected;
      }
      if (seen.has(next)) {
        log.warning(`downstream repeated cursor ${JSON.stringify(next)}; stopping`);
        return collected;
      }
      seen.add(next);
      cursor = next;
    }
    log.warning(`downstream listing exceeded ${MAX_LIST_PAGES} pages; stopping`);
    return collected;
  }

  mountedTools(): Tool[] {
    return [...[...this.#tools.values()].map((entry) => entry.exposed), ...[...this.#collections.values()].map((entry) => entry.exposed)];
  }

  resolve(mounted: string): MountedTool | undefined {
    return this.#tools.get(mounted);
  }

  resolveCollection(mounted: string): MountedCollection | undefined {
    return this.#collections.get(mounted);
  }

  fetchCollection(
    entry: MountedCollection,
    args: JsonObject | null,
    capabilities?: JsonValue,
    cancel?: AbortSignal,
  ): Promise<FetchOutcome> {
    return fetchAll(
      (pageArgs, signal) =>
        this.#callOriginal(entry.original, { name: entry.original.name, arguments: pageArgs, capabilities }, signal),
      entry.config,
      args,
      cancel,
    );
  }

  call(mounted: string, params: Omit<CallParams, "name">, signal?: AbortSignal): Promise<DownstreamReply> {
    const entry = this.resolve(mounted);
    if (entry === undefined) {
      return Promise.reject(new DownstreamError(FailureClass.PROTOCOL, mounted, `no such tool: ${mounted}`));
    }
    return this.#callOriginal(entry.original, { ...params, name: entry.original.name }, signal);
  }

  async #callOriginal(tool: Tool, params: CallParams, signal?: AbortSignal): Promise<DownstreamReply> {
    if (!this.#healthy) {
      throw new DownstreamError(
        FailureClass.TRANSPORT,
        params.name,
        "downstream session is unhealthy after an earlier transport failure",
      );
    }
    try {
      return await this.#session.callTool(params, { toolDefinition: tool, signal });
    } catch (error) {
      if (signal?.aborted === true) {
        throw error;
      }
      if (error instanceof SdkError && TRANSPORT_CODES.has(error.code)) {
        this.#healthy = false;
        throw new DownstreamError(FailureClass.TRANSPORT, params.name, `${error.name}: ${error.message}`);
      }
      if (error instanceof ProtocolError || error instanceof SdkError) {
        const text = error.message;
        const lowered = text.toLowerCase();
        if (OUTPUT_SCHEMA_MARKERS.some((marker) => lowered.includes(marker))) {
          throw new DownstreamError(FailureClass.OUTPUT_SCHEMA, params.name, text);
        }
        throw new DownstreamError(FailureClass.PROTOCOL, params.name, text);
      }
      throw error;
    }
  }
}

export function argumentsOf(value: JsonValue | undefined): JsonObject | null {
  return isJsonObject(value) ? value : null;
}
