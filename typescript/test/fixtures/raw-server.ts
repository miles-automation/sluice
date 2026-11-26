import { createInterface } from "node:readline";
import { isJsonObject, parseJson, type JsonObject, type JsonValue } from "../../src/json.ts";

const OUTPUT_SCHEMA = {
  type: "object",
  properties: { v: { type: "integer", maximum: 10 } },
  required: ["v"],
};

const TOOLS = [
  { name: "duplicate", description: "answers one call with two responses", inputSchema: { type: "object", properties: {} }, outputSchema: OUTPUT_SCHEMA },
  { name: "malformed", description: "answers with a line that is not JSON", inputSchema: { type: "object", properties: {} } },
  { name: "stray", description: "sends a response for an id that is not in flight first", inputSchema: { type: "object", properties: {} } },
];

function write(text: string): void {
  process.stdout.write(text);
}

function respond(id: JsonValue, result: unknown): void {
  write(`${JSON.stringify({ jsonrpc: "2.0", id: typeof id === "bigint" ? Number(id) : id, result })}\n`);
}

function call(id: JsonValue, params: JsonObject): void {
  const name = params.get("name");
  const numeric = typeof id === "bigint" ? id.toString() : JSON.stringify(id);
  if (name === "duplicate") {
    write(
      `{"jsonrpc":"2.0","id":"${numeric}","result":{"content":[{"type":"text","text":"first"}],"structuredContent":{"v":1}}}\n` +
        `{"jsonrpc":"2.0","id":${numeric},"result":{"content":[{"type":"text","text":"second"}],"structuredContent":{"v":999,"evil":"INJECTED"}}}\n`,
    );
    return;
  }
  if (name === "malformed") {
    write(`{"jsonrpc":"2.0","id":${numeric},"result":{"content":[{"type":"text","text":"cut off\n`);
    return;
  }
  if (name === "stray") {
    write(`{"jsonrpc":"2.0","id":987654,"result":{"content":[{"type":"text","text":"stray"}]}}\n`);
    respond(id, { content: [{ type: "text", text: "{\"items\": [{\"ok\": true}]}" }] });
    return;
  }
  respond(id, { content: [{ type: "text", text: `unknown tool ${String(name)}` }], isError: true });
}

const lines = createInterface({ input: process.stdin });
lines.on("line", (line) => {
  const message = parseJson(line);
  if (!isJsonObject(message)) {
    return;
  }
  const id = message.get("id");
  const method = message.get("method");
  const params = message.get("params");
  if (id === undefined || typeof method !== "string") {
    return;
  }
  if (method === "initialize") {
    const requested = isJsonObject(params) ? params.get("protocolVersion") : undefined;
    respond(id, {
      protocolVersion: typeof requested === "string" ? requested : "2025-11-25",
      capabilities: { tools: {} },
      serverInfo: { name: "raw", version: "0" },
    });
  } else if (method === "tools/list") {
    respond(id, { tools: TOOLS });
  } else if (method === "tools/call" && isJsonObject(params)) {
    call(id, params);
  } else {
    write(`${JSON.stringify({ jsonrpc: "2.0", id: typeof id === "bigint" ? Number(id) : id, error: { code: -32601, message: "Method not found" } })}\n`);
  }
});
