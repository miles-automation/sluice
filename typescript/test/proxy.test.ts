import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  Client,
  LATEST_PROTOCOL_VERSION,
  SUPPORTED_PROTOCOL_VERSIONS,
  SdkError,
  SdkErrorCode,
  type CallToolResult,
} from "@modelcontextprotocol/client";
import { DownstreamError, FailureClass } from "../src/errors.ts";
import { isStartupError } from "../src/main.ts";
import { NameCollisionError, mountedName } from "../src/naming.ts";
import type { DownstreamReply, FinalResult } from "../src/paginate.ts";
import { HANDLE_NOTE, MODERN_VERSIONS, Proxy, type Session } from "../src/proxy.ts";
import { parseJson } from "../src/json.ts";
import { DownstreamTransport } from "../src/transport.ts";
import { rawJson, toWire } from "../src/wire.ts";
import { PAGE_TWO_TOOL } from "./fixtures/fake-server.ts";
import { FAKE_SERVER, PACKAGE_ROOT, fakeConfig, sqlRows, structured, text, withProxy, withSluice } from "./support.ts";

const mounted = (tool: string): string => mountedName("fake", tool);
const config = fakeConfig();

function final(reply: DownstreamReply): FinalResult {
  assert.equal(reply.kind, "result");
  return reply as FinalResult;
}

async function direct<T>(work: (client: Client) => Promise<T>): Promise<T> {
  const transport = new DownstreamTransport({ command: process.execPath, args: [FAKE_SERVER], env: {}, cwd: PACKAGE_ROOT });
  const client = new Client({ name: "direct", version: "0" }, { versionNegotiation: { mode: "auto" }, inputRequired: { autoFulfill: false } });
  await client.connect(transport);
  try {
    return await work(client);
  } finally {
    await client.close();
  }
}

describe("proxy: listing and cloning", () => {
  test("a tool reachable only on page two is mounted", () =>
    withProxy(config, async (proxy) => {
      assert.ok(proxy.mountedTools().some((tool) => tool.name === mounted(PAGE_TWO_TOOL)));
    }));

  test("the SDK's per-page tools/list returns one page", () =>
    direct(async (client) => {
      const page = await client.request({ method: "tools/list", params: {} });
      assert.notEqual(page.nextCursor, undefined);
      assert.ok(!page.tools.some((tool) => tool.name === PAGE_TWO_TOOL));
    }));

  test("colliding tool names are both mounted", () =>
    withProxy(config, async (proxy) => {
      const names = new Set(proxy.mountedTools().map((tool) => tool.name));
      assert.ok(names.has(mounted("hyphen-tool")));
      assert.ok(names.has(mounted("hyphen_tool")));
      assert.notEqual(mounted("hyphen-tool"), mounted("hyphen_tool"));
    }));

  test("annotations, title and _meta survive cloning", () =>
    withProxy(config, async (proxy) => {
      const tool = proxy.mountedTools().find((candidate) => candidate.name === mounted("destructive"));
      assert.equal(tool?.annotations?.destructiveHint, true);
      assert.equal(tool?.annotations?.title, "Destructive");
      assert.equal(tool?.title, "Destructive Thing");
      assert.deepEqual(tool?._meta, { "vendor.example/owner": "fake" });
    }));

  test("the output schema is removed", () =>
    withProxy(config, async (proxy) => {
      const entry = proxy.resolve(mounted("bad_schema"));
      assert.notEqual(entry?.original.outputSchema, undefined);
      assert.ok(entry !== undefined && !("outputSchema" in entry.exposed));
    }));

  test("the description keeps the upstream text and appends the note", () =>
    withProxy(config, async (proxy) => {
      const tool = proxy.mountedTools().find((candidate) => candidate.name === mounted("rows"));
      assert.ok(tool?.description?.startsWith("n homogeneous objects"));
      assert.ok(tool?.description?.endsWith(HANDLE_NOTE));
    }));

  test("the input schema is passed through", () =>
    withProxy(config, async (proxy) => {
      const entry = proxy.resolve(mounted("rows"));
      assert.deepEqual(entry?.exposed.inputSchema, entry?.original.inputSchema);
    }));

  test("startup fails loudly on a mounted-name collision", async () => {
    await assert.rejects(Proxy.start(config, { mount: () => "constant" }), (error: unknown) => {
      assert.ok(error instanceof NameCollisionError);
      assert.ok(isStartupError(error));
      assert.match(error.message, /both mount as/u);
      return true;
    });
  });
});

describe("proxy: calls and failures", () => {
  test("a call is forwarded and returns the payload", () =>
    withProxy(config, async (proxy) => {
      const reply = final(await proxy.call(mounted("rows"), { arguments: new Map([["n", 5n]]) }));
      const items = (JSON.parse(text(reply.result as CallToolResult)) as { items: unknown[] }).items;
      assert.equal(items.length, 5);
    }));

  test("an unknown tool is a protocol failure", () =>
    withProxy(config, async (proxy) => {
      await assert.rejects(proxy.call("no_such_tool", { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.PROTOCOL);
        return true;
      });
    }));

  test("an output-schema violation is classified", () =>
    withProxy(config, async (proxy) => {
      await assert.rejects(proxy.call(mounted("bad_schema"), { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.OUTPUT_SCHEMA);
        return true;
      });
    }));

  test("an output schema the SDK cannot compile never fails the call", () =>
    withSluice(config, async ({ client }) => {
      for (const tool of ["draft4_schema", "unversioned_schema"]) {
        const result = await client.callTool({ name: mounted(tool) });
        assert.notEqual(result.isError, true, text(result));
        assert.ok(text(result).includes("sluice: result recorded."), text(result));
      }
    }));

  test("the SDK's output-schema error carries a classifiable message", () =>
    direct(async (client) => {
      const page = await client.request({ method: "tools/list", params: {} });
      const definition = page.tools.find((tool) => tool.name === "bad_schema");
      assert.ok(definition !== undefined);
      await assert.rejects(client.callTool({ name: "bad_schema" }, { toolDefinition: definition }), (error: unknown) => {
        assert.ok(error instanceof Error);
        const message = error.message.toLowerCase();
        assert.ok(message.includes("output schema") || message.includes("structured content"), message);
        return true;
      });
    }));

  test("a transport failure marks the session unhealthy", () =>
    withProxy(config, async (real) => {
      let calls = 0;
      const broken: Session = {
        get protocolVersion() {
          return real.session.protocolVersion;
        },
        listTools: (cursor) => real.session.listTools(cursor),
        callTool: () => {
          calls += 1;
          return Promise.reject(new SdkError(SdkErrorCode.ConnectionClosed, "Connection closed"));
        },
        close: () => Promise.resolve(),
      };
      const proxy = new Proxy(config, broken);
      await proxy.refreshTools();
      await assert.rejects(proxy.call(mounted("rows"), { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.TRANSPORT);
        return true;
      });
      assert.equal(proxy.healthy, false);
      await assert.rejects(proxy.call(mounted("rows"), { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.TRANSPORT);
        assert.match(error.message, /session is unhealthy/u);
        return true;
      });
      assert.equal(calls, 1);
    }));

  test("a downstream that dies is a transport failure, and later calls fail fast", () =>
    withProxy(config, async (proxy) => {
      await assert.rejects(proxy.call(mounted("die"), { arguments: null }), (error: unknown) => {
        assert.ok(error instanceof DownstreamError);
        assert.equal(error.failureClass, FailureClass.TRANSPORT);
        return true;
      });
      assert.equal(proxy.healthy, false);
    }));

  test("round trips are relayed, not answered", () =>
    withProxy(config, async (proxy) => {
      const capabilities = new Map([["elicitation", new Map([["form", new Map()]])]]);
      const first = await proxy.call(mounted("needs_input"), { arguments: null, capabilities });
      assert.equal(first.kind, "input_required");
      assert.equal(first.result["requestState"], "awaiting-pick");
      assert.ok(Object.hasOwn(first.result["inputRequests"] as object, "pick"));
      const responses = new Map([["pick", new Map<string, never>([["action", "accept" as never], ["content", new Map([["n", 7n]]) as never]])]]);
      const second = final(
        await proxy.call(mounted("needs_input"), {
          arguments: null,
          capabilities,
          requestState: "awaiting-pick",
          inputResponses: responses,
        }),
      );
      const item = (JSON.parse(text(second.result as CallToolResult)) as { items: Record<string, unknown>[] }).items[0];
      assert.equal(item?.["round"], 2);
      assert.equal(item?.["state"], "awaiting-pick");
      assert.deepEqual(JSON.parse(String(item?.["answer"])), { pick: { action: "accept", content: { n: 7 } } });
    }));

  test("Sluice advertises no sampling or elicitation of its own", () =>
    withProxy(config, async (proxy) => {
      const reply = final(await proxy.call(mounted("client_capabilities"), { arguments: null }));
      assert.deepEqual(JSON.parse(text(reply.result as CallToolResult)), { sampling: false, elicitation: false });
    }));
});

describe("proxy: protocol versions", () => {
  test("the initialize handshake tops out below the round-trip version", () => {
    assert.equal(LATEST_PROTOCOL_VERSION, "2025-11-25");
    for (const version of MODERN_VERSIONS) {
      assert.ok(!SUPPORTED_PROTOCOL_VERSIONS.includes(version));
    }
  });

  test("discover negotiates the modern version with a modern downstream", () =>
    withProxy(config, async (proxy) => {
      assert.equal(proxy.protocolVersion, "2026-07-28");
      assert.equal(proxy.supportsRoundTrips, true);
    }));

  test("a downstream that only speaks the initialize handshake still works", () =>
    withProxy(fakeConfig({ legacy: true }), async (proxy) => {
      assert.equal(proxy.protocolVersion, "2025-11-25");
      assert.equal(proxy.supportsRoundTrips, false);
      const reply = final(await proxy.call(mounted("rows"), { arguments: new Map([["n", 3n]]) }));
      assert.equal((JSON.parse(text(reply.result as CallToolResult)) as { items: unknown[] }).items.length, 3);
    }));

  test("an input request is refused with the versions named when the upstream client is too old", () =>
    withSluice(
      config,
      async ({ client }) => {
        assert.equal(client.getNegotiatedProtocolVersion(), "2025-11-25");
        const result = await client.callTool({ name: mounted("needs_input") });
        assert.equal(result.isError, true);
        assert.match(text(result), /requires protocol 2026-07-28; this client negotiated 2025-11-25/u);
      },
      { mode: "legacy", capabilities: { elicitation: { form: {} } } },
    ));

  test("a modern upstream client gets the input request relayed with its own capabilities", () =>
    withSluice(
      config,
      async ({ client }) => {
        assert.equal(client.getNegotiatedProtocolVersion(), "2026-07-28");
        const first = (await client.callTool({ name: mounted("needs_input") }, { allowInputRequired: true })) as unknown as Record<string, unknown>;
        assert.equal(first["resultType"], "input_required");
        assert.equal(first["requestState"], "awaiting-pick");
        const second = await client.callTool(
          {
            name: mounted("needs_input"),
            inputResponses: { pick: { action: "accept", content: { n: 7 } } },
            requestState: "awaiting-pick",
          } as unknown as Parameters<Client["callTool"]>[0],
          { allowInputRequired: true },
        );
        assert.match(text(second), /sluice: result recorded\./u);
      },
      { capabilities: { elicitation: { form: {} } } },
    ));
});

const PASSTHROUGH_TOOLS = ["rows", "boom", "picture", "just_text", "structured_only", "both_channels", "rich_result"];

describe("passthrough", () => {
  test("a result through the proxy matches a direct call", () =>
    withProxy(config, async (proxy) => {
      const through: unknown[] = [];
      for (const tool of PASSTHROUGH_TOOLS) {
        through.push(final(await proxy.call(mounted(tool), { arguments: new Map([["n", 5n]]) })).result);
      }
      const directly = await direct(async (client) => {
        const results: unknown[] = [];
        for (const tool of PASSTHROUGH_TOOLS) {
          results.push(await client.callTool({ name: tool, arguments: { n: 5 } }));
        }
        return results;
      });
      assert.deepEqual(through, directly);
    }));

  test("result metadata and content annotations survive the hop", () =>
    withProxy(config, async (proxy) => {
      const { result } = final(await proxy.call(mounted("rich_result"), { arguments: null }));
      assert.equal((result["_meta"] as Record<string, unknown>)["vendor.example/requestId"], "req-42");
      const [block] = result.content;
      assert.deepEqual(block?.["_meta"], { "vendor.example/trace": "abc123" });
      assert.deepEqual(block?.["annotations"], { audience: ["user"], priority: 0.7 });
    }));

  test("errors are forwarded verbatim", () =>
    withProxy(config, async (proxy) => {
      const { result } = final(await proxy.call(mounted("boom"), { arguments: null }));
      assert.equal(result.isError, true);
      assert.equal(result.content[0]?.text, "downstream says no");
    }));

  test("image content is not materialized", () =>
    withProxy(config, async (proxy) => {
      const { result } = final(await proxy.call(mounted("picture"), { arguments: null }));
      assert.equal(result.content[0]?.type, "image");
    }));

  test("the full loop through the Sluice server", () =>
    withSluice(
      config,
      async ({ client }) => {
        const listing = await client.listTools();
        const tool = listing.tools.find((candidate) => candidate.name === mounted("rows"));
        assert.ok(tool !== undefined);
        assert.equal(tool.outputSchema, undefined);
        const result = await client.callTool({ name: tool.name, arguments: { n: 3 } });
        assert.ok(text(result).includes('"items"'));
      },
      { interceptor: false, query: false },
    ));

  test("the query tool is mounted and marked read-only", () =>
    withSluice(config, async ({ client }) => {
      const listing = await client.listTools();
      const tool = listing.tools.find((candidate) => candidate.name === "query");
      assert.equal(tool?.annotations?.readOnlyHint, true);
      assert.equal(tool?.annotations?.destructiveHint, false);
      assert.deepEqual(tool?.inputSchema.required, ["sql"]);
    }));

  test("the full loop returns a handle, not a payload", () =>
    withSluice(config, async ({ client }) => {
      const result = await client.callTool({ name: mounted("rows"), arguments: { n: 400 } });
      assert.ok(text(result).includes("sluice: result recorded."));
      assert.ok(!text(result).includes("row-0399"));
      assert.ok(structured(result)["call_id"]);
    }));

  test("error metadata survives the whole product path", () =>
    withSluice(config, async ({ client }) => {
      const result = await client.callTool({ name: mounted("boom") });
      assert.equal(result.isError, true);
      assert.equal((result._meta as Record<string, unknown>)["vendor.example/errorCode"], "E_NOPE");
      assert.equal(text(result), "downstream says no");
    }));

  test("image metadata survives the whole product path", () =>
    withSluice(config, async ({ client }) => {
      const result = await client.callTool({ name: mounted("picture") });
      assert.equal(result.content[0]?.type, "image");
      assert.equal((result._meta as Record<string, unknown>)["vendor.example/assetId"], "img-7");
      assert.equal(result.structuredContent, undefined);
    }));

  test("non-JSON rich text is recorded as an envelope-only handle", () =>
    withSluice(config, async ({ client }) => {
      const result = await client.callTool({ name: mounted("rich_result") });
      assert.ok(String(structured(result)["flat_reason"]).startsWith("not_json"));
    }));

  test("non-forwardable failures are recorded with their class", () =>
    withSluice(config, async ({ client, store }) => {
      const schemaFailure = await client.callTool({ name: mounted("bad_schema") });
      const protocolFailure = await client.callTool({ name: "fake__missing" });
      assert.equal(schemaFailure.isError, true);
      assert.equal(protocolFailure.isError, true);
      const rows = await sqlRows(store, "SELECT tool, failure_class, is_error FROM sluice_calls ORDER BY seq");
      assert.deepEqual(rows, [
        ["bad_schema", "output_schema", true],
        ["fake__missing", "protocol", true],
      ]);
    }));
});

const BIG = "9223372036854775807";

function rawResult(wire: readonly string[], marker: string): string {
  const line = wire.find((candidate) => candidate.includes(marker) && candidate.includes('"result"'));
  assert.ok(line !== undefined, `no response containing ${marker}`);
  return line;
}

describe("lossless structured content", () => {
  test("integers past 2^53 and 1.0 reach the table exactly", () =>
    withSluice(config, async ({ client }) => {
      const result = await client.callTool({ name: mounted("lossless") });
      const [table] = structured(result)["tables"] as Record<string, unknown>[];
      const query = await client.callTool({
        name: "query",
        arguments: { sql: `SELECT big::VARCHAR AS big, mid::VARCHAR AS mid, f, typeof(f) AS t, neg::VARCHAR AS neg FROM "${String(table?.["name"])}"` },
      });
      assert.ok(text(query).includes(`| ${BIG} | 9007199254740993 | 1 | DOUBLE | -9223372036854775808 |`), text(query));
    }));

  test("a passed-through result keeps its structured numbers on the wire", () =>
    withSluice(config, async ({ client, wire }) => {
      const result = await client.callTool({ name: mounted("lossless_error") });
      assert.equal(result.isError, true);
      const line = rawResult(wire, '"text":"failed"');
      assert.ok(line.includes(`"big":${BIG}`), line);
      assert.ok(line.includes('"mid":9007199254740993'), line);
      assert.ok(line.includes('"f":1.0'), line);
    }));

  test("arguments past 2^53 reach the envelope unrounded and are re-sent exactly", () =>
    withSluice(config, async ({ client, store }) => {
      const result = await client.callTool({ name: mounted("echo_args"), arguments: { id: rawJson(BIG), f: rawJson("1.0") } });
      assert.notEqual(result.isError, true);
      const rows = await sqlRows(store, "SELECT args FROM sluice_calls ORDER BY seq DESC LIMIT 1");
      assert.equal(rows[0]?.[0], `{"id": ${BIG}, "f": 1.0}`);
      const parsed = parseJson(`{"id": ${BIG}, "f": 1.0, "small": 3, "half": 0.5, "__proto__": 1}`);
      assert.equal(JSON.stringify(toWire(parsed)), `{"id":${BIG},"f":1.0,"small":3,"half":0.5,"__proto__":1}`);
    }));
});
