import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, test } from "node:test";
import { promisify } from "node:util";
import { Client } from "@modelcontextprotocol/client";
import { CONFIG_ENV_VAR, DEFAULT_CONFIG_NAME, findConfig } from "../src/config.ts";
import { mountedName } from "../src/naming.ts";
import { CLI, FAKE_SERVER, PACKAGE_ROOT, StreamClientTransport, structured, text } from "./support.ts";

const run = promisify(execFile);
const directory = mkdtempSync(join(tmpdir(), "sluice-cli-"));
after(() => rmSync(directory, { recursive: true, force: true }));

function writeConfig(name: string, body: string): string {
  const path = join(directory, name);
  writeFileSync(path, body, "utf8");
  return path;
}

function fakeServerConfig(name: string, command: string = process.execPath, extra = ""): string {
  return writeConfig(
    name,
    `[servers.fake]\ncommand = ${JSON.stringify(command)}\nargs = [${JSON.stringify(FAKE_SERVER)}]\ncwd = ${JSON.stringify(PACKAGE_ROOT)}\n\n[limits]\npreview_bytes = 512\n${extra}`,
  );
}

interface Exit {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
}

async function sluice(args: readonly string[], options: { readonly cwd?: string; readonly env?: NodeJS.ProcessEnv } = {}): Promise<Exit> {
  try {
    const { stdout, stderr } = await run(process.execPath, [CLI, ...args], {
      cwd: options.cwd ?? PACKAGE_ROOT,
      env: options.env ?? process.env,
      timeout: 60_000,
    });
    return { code: 0, stdout, stderr };
  } catch (error) {
    const failed = error as { code?: number; stdout?: string; stderr?: string };
    return { code: failed.code ?? -1, stdout: failed.stdout ?? "", stderr: failed.stderr ?? "" };
  }
}

describe("cli", () => {
  test("Sluice runs as a real stdio server and writes only MCP to stdout", async () => {
    const config = fakeServerConfig("real.toml");
    const child = spawn(process.execPath, [CLI, "--config", config, "--log-level", "debug"], {
      cwd: PACKAGE_ROOT,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    const exited = new Promise<number | null>((resolve) => child.once("exit", resolve));
    const transport = new StreamClientTransport(child.stdout, child.stdin);
    const client = new Client({ name: "cli-test", version: "0" }, { versionNegotiation: { mode: "auto" } });
    await client.connect(transport);
    const listing = await client.listTools();
    const names = new Set(listing.tools.map((tool) => tool.name));
    const rows = mountedName("fake", "rows");
    assert.ok(names.has(rows));
    assert.ok(names.has("query"));
    const result = await client.callTool({ name: rows, arguments: { n: 400 } });
    assert.ok(text(result).includes("sluice: result recorded."));
    assert.ok(!text(result).includes("row-0399"));
    const [table] = structured(result)["tables"] as Record<string, unknown>[];
    assert.equal(table?.["row_count"], 400);
    assert.ok(text(result).includes("`query` tool"));
    const queried = await client.callTool({ name: "query", arguments: { sql: `SELECT count(*) FROM "${String(table?.["name"])}"` } });
    assert.notEqual(queried.isError, true);
    assert.ok(text(queried).includes("| 400 |"));
    const blocked = await client.callTool({ name: "query", arguments: { sql: "SELECT * FROM sluice_calls" } });
    assert.equal(blocked.isError, true);
    await client.close();
    assert.equal(await exited, 0);
    const written = Buffer.concat(stdout).toString("utf8");
    assert.equal(written, transport.lines.map((line) => `${line}\n`).join(""));
    for (const line of transport.lines) {
      assert.equal((JSON.parse(line) as { jsonrpc?: unknown }).jsonrpc, "2.0");
    }
    const logs = Buffer.concat(stderr).toString("utf8");
    assert.match(logs, /INFO sluice\.proxy: mounted/u);
  });

  test("a missing config exits with a diagnostic", async () => {
    const exit = await sluice(["--config", join(directory, "absent.toml")]);
    assert.equal(exit.code, 2);
    assert.ok(exit.stderr.includes("no config file"));
    assert.equal(exit.stdout, "");
  });

  test("invalid limits exit with a diagnostic", async () => {
    const path = writeConfig("bad-limits.toml", '[servers.fake]\ncommand = "echo"\n\n[limits]\nmax_concurrent_materializations = 0\n');
    const exit = await sluice(["--config", path]);
    assert.equal(exit.code, 2);
    assert.ok(exit.stderr.includes("max_concurrent_materializations"));
    assert.equal(exit.stdout, "");
  });

  test("an unreachable downstream fails to start", async () => {
    const exit = await sluice(["--config", fakeServerConfig("unreachable.toml", "definitely-not-a-real-command")]);
    assert.equal(exit.code, 1);
    assert.ok(exit.stderr.includes("could not start downstream"), exit.stderr);
    assert.doesNotMatch(exit.stderr, /\n\s+at /u);
    assert.equal(exit.stdout, "");
  });

  test("a refused pagination entry fails to start", async () => {
    const exit = await sluice(["--config", fakeServerConfig("mutable.toml", process.execPath, "\n[pagination.paged_mutable]\n")]);
    assert.equal(exit.code, 1);
    assert.ok(exit.stderr.includes("readOnlyHint"), exit.stderr);
    assert.equal(exit.stdout, "");
  });

  test("--help prints usage and exits", async () => {
    const exit = await sluice(["--help"]);
    assert.equal(exit.code, 0);
    assert.ok(exit.stdout.startsWith("usage: sluice [-h] [--config CONFIG] [--log-level LOG_LEVEL]"));
    assert.ok(exit.stdout.includes("--config CONFIG       path to sluice.toml"));
  });

  test("an unknown flag is a usage error", async () => {
    const exit = await sluice(["--nope"]);
    assert.equal(exit.code, 2);
    assert.ok(exit.stderr.includes("usage: sluice"));
    assert.equal(exit.stdout, "");
  });

  test("config discovery: --config, then SLUICE_CONFIG, then ./sluice.toml", async () => {
    const explicit = join(directory, "explicit.toml");
    const fromEnv = join(directory, "from-env.toml");
    assert.equal(findConfig(explicit, { [CONFIG_ENV_VAR]: fromEnv }), explicit);
    assert.equal(findConfig(null, { [CONFIG_ENV_VAR]: fromEnv }), fromEnv);
    assert.equal(findConfig(null, {}), DEFAULT_CONFIG_NAME);
    const viaEnv = await sluice([], { env: { ...process.env, [CONFIG_ENV_VAR]: fromEnv } });
    assert.equal(viaEnv.code, 2);
    assert.ok(viaEnv.stderr.includes(`no config file at ${fromEnv}`));
    const environment = { ...process.env };
    delete environment[CONFIG_ENV_VAR];
    const viaCwd = await sluice([], { cwd: directory, env: environment });
    assert.equal(viaCwd.code, 2);
    assert.ok(viaCwd.stderr.includes("no config file at sluice.toml"));
  });
});
