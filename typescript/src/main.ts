import { parseArgs } from "node:util";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import { ConfigError, findConfig, loadConfig, type Config } from "./config.ts";
import { DownstreamError } from "./errors.ts";
import { Interceptor } from "./intercept.ts";
import { isLevelName, logger, setLevel } from "./log.ts";
import { NameCollisionError } from "./naming.ts";
import { Proxy } from "./proxy.ts";
import { QueryTool } from "./query.ts";
import { buildServer } from "./server.ts";
import { Store } from "./store.ts";
import { UpstreamTransport } from "./transport.ts";

const log = logger("sluice");

export const USAGE = "usage: sluice [-h] [--config CONFIG] [--log-level LOG_LEVEL]";

export const HELP = `${USAGE}

Entrypoint: \`sluice --config sluice.toml\`, speaking MCP over stdio.

options:
  -h, --help            show this help message and exit
  --config CONFIG       path to sluice.toml
  --log-level LOG_LEVEL
`;

export function isStartupError(error: unknown): error is Error {
  return error instanceof DownstreamError || error instanceof NameCollisionError || error instanceof ConfigError;
}

export interface Arguments {
  readonly config: string | null;
  readonly logLevel: string;
  readonly help: boolean;
}

export function parseArguments(argv: readonly string[]): Arguments {
  const { values } = parseArgs({
    args: [...argv],
    options: {
      config: { type: "string" },
      "log-level": { type: "string", default: "INFO" },
      help: { type: "boolean", short: "h", default: false },
    },
    strict: true,
    allowPositionals: false,
  });
  return { config: values.config ?? null, logLevel: values["log-level"], help: values.help };
}

export async function serve(config: Config): Promise<void> {
  const proxy = await Proxy.start(config);
  let store: Store | undefined;
  try {
    store = await Store.open(config.limits);
    const interceptor = new Interceptor(store, config.limits, { queryAvailable: true });
    const query = new QueryTool(store, config.limits);
    const transport = new UpstreamTransport();
    const handle = serveStdio(
      () => buildServer({ proxy, interceptor, query, requests: (id) => transport.takeRequest(id) }),
      { transport, onerror: (error) => log.debug(`upstream: ${error.message}`) },
    );
    const interrupted = new Promise<void>((resolve) => process.once("SIGINT", () => resolve()));
    await Promise.race([transport.closed, interrupted]);
    await handle.close();
  } finally {
    store?.close();
    await proxy.close();
  }
}

export async function main(argv: readonly string[]): Promise<number> {
  let args: Arguments;
  try {
    args = parseArguments(argv);
  } catch (error) {
    process.stderr.write(`${USAGE}\nsluice: error: ${error instanceof Error ? error.message : String(error)}\n`);
    return 2;
  }
  if (args.help) {
    process.stdout.write(HELP);
    return 0;
  }
  const level = args.logLevel.toUpperCase();
  if (!isLevelName(level)) {
    process.stderr.write(`${USAGE}\nsluice: error: unknown log level ${JSON.stringify(args.logLevel)}\n`);
    return 2;
  }
  setLevel(level);
  let config: Config;
  try {
    config = loadConfig(findConfig(args.config));
  } catch (error) {
    if (error instanceof ConfigError) {
      process.stderr.write(`sluice: ${error.message}\n`);
      return 2;
    }
    throw error;
  }
  let interrupted = false;
  process.once("SIGINT", () => {
    interrupted = true;
  });
  try {
    await serve(config);
  } catch (error) {
    if (!isStartupError(error)) {
      throw error;
    }
    process.stderr.write(`sluice: ${error.message}\n`);
    return 1;
  }
  return interrupted ? 130 : 0;
}
