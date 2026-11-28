import { existsSync, readFileSync } from "node:fs";
import { parse as parseToml } from "smol-toml";
import { pyRepr, pyStr } from "./pystr.ts";
import { DEFAULT_LIMITS, LimitsError, makeLimits, type Limits } from "./limits.ts";

export const CONFIG_ENV_VAR = "SLUICE_CONFIG";
export const DEFAULT_CONFIG_NAME = "sluice.toml";
export const PAGINATION_DEFAULT_MAX_BYTES = 8 * 1024 * 1024;

const VAR = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/gu;

export class ConfigError extends Error {
  override readonly name = "ConfigError";
}

export interface ServerConfig {
  readonly name: string;
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
  readonly cwd: string | null;
}

export interface PaginationConfig {
  readonly tool: string;
  readonly items: string;
  readonly limit_arg: string;
  readonly offset_arg: string;
  readonly has_more: string;
  readonly next_offset: string;
  readonly page_size: number;
  readonly max_pages: number;
  readonly max_bytes: number;
  readonly max_seconds: number;
}

export interface Config {
  readonly server: ServerConfig;
  readonly limits: Limits;
  readonly pagination: ReadonlyMap<string, PaginationConfig>;
}

type Environment = Readonly<Record<string, string | undefined>>;
type Table = Readonly<Record<string, unknown>>;

const PAGINATION_DEFAULTS = {
  items: "items",
  limit_arg: "limit",
  offset_arg: "offset",
  has_more: "has_more",
  next_offset: "next_offset",
  page_size: 200,
  max_pages: 100,
  max_bytes: PAGINATION_DEFAULT_MAX_BYTES,
  max_seconds: 120,
} as const;

const PAGINATION_NAMES = ["items", "limit_arg", "offset_arg", "has_more", "next_offset"] as const;
const PAGINATION_COUNTS = ["page_size", "max_pages", "max_bytes"] as const;
const INTEGER_LIMITS = [
  "max_payload_bytes",
  "max_concurrent_materializations",
  "max_columns",
  "query_max_rows",
  "query_max_bytes",
  "max_cell_bytes",
  "max_session_bytes",
  "max_session_calls",
  "store_max_memory",
  "preview_bytes",
  "preview_rows",
] as const;

function isTable(value: unknown): value is Table {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof Date);
}

function typeName(value: unknown): string {
  if (typeof value === "bigint") {
    return "int";
  }
  if (typeof value === "number") {
    return "float";
  }
  if (typeof value === "boolean") {
    return "bool";
  }
  if (typeof value === "string") {
    return "str";
  }
  if (Array.isArray(value)) {
    return "list";
  }
  return isTable(value) ? "dict" : "datetime";
}

const repr = pyRepr;

const SAFE_MAX = BigInt(Number.MAX_SAFE_INTEGER);
const SAFE_MIN = BigInt(Number.MIN_SAFE_INTEGER);

function clamp(value: bigint): number {
  return Number(value > SAFE_MAX ? SAFE_MAX : value < SAFE_MIN ? SAFE_MIN : value);
}

export function findConfig(explicit: string | null = null, environ: Environment = process.env): string {
  if (explicit !== null) {
    return explicit;
  }
  const fromEnv = environ[CONFIG_ENV_VAR];
  if (fromEnv !== undefined && fromEnv !== "") {
    return fromEnv;
  }
  return DEFAULT_CONFIG_NAME;
}

export function expandVars(value: string, environ: Environment = process.env): string {
  return value.replace(VAR, (_match, name: string) => {
    const found = environ[name];
    if (found === undefined) {
      throw new ConfigError(`config references \${${name}}, which is not set`);
    }
    return found;
  });
}

export function loadConfig(path: string, environ: Environment = process.env): Config {
  if (!existsSync(path)) {
    throw new ConfigError(`no config file at ${path}`);
  }
  let raw: Table;
  try {
    const text = readFileSync(path, "utf8");
    if (text.startsWith("\ufeff")) {
      throw new Error("Invalid statement (at line 1, column 1)");
    }
    raw = parseToml(text, { integersAsBigInt: true });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new ConfigError(`${path} is not valid TOML: ${detail}`);
  }
  return parseConfig(raw, environ);
}

function parseServer(raw: Table, environ: Environment): ServerConfig {
  const servers = raw["servers"];
  if (!isTable(servers) || Object.keys(servers).length === 0) {
    throw new ConfigError("config must define at least one [servers.<name>] table");
  }
  const names = Object.keys(servers);
  if (names.length > 1) {
    throw new ConfigError(
      `v0 proxies exactly one downstream server; found ${names.length} (${[...names].sort().join(", ")}). ` +
        "Tool namespacing is built for fan-out, but fan-out is not implemented.",
    );
  }
  const [name = ""] = names;
  const entry = servers[name];
  if (!isTable(entry)) {
    throw new ConfigError(`[servers.${name}] must be a table`);
  }
  const command = entry["command"];
  const url = entry["url"];
  if (command === undefined && url === undefined) {
    throw new ConfigError(`[servers.${name}] needs either \`command\` or \`url\``);
  }
  if (command !== undefined && url !== undefined) {
    throw new ConfigError(`[servers.${name}] sets both \`command\` and \`url\`; they are exclusive`);
  }
  if (url !== undefined) {
    throw new ConfigError(`[servers.${name}] sets \`url\`; v0 speaks stdio only, so \`command\` is required`);
  }
  if (typeof command !== "string") {
    throw new ConfigError(`[servers.${name}].command must be a string`);
  }
  const args = entry["args"] ?? [];
  if (!Array.isArray(args) || !args.every((arg): arg is string => typeof arg === "string")) {
    throw new ConfigError(`[servers.${name}].args must be a list of strings`);
  }
  const envRaw = entry["env"] ?? {};
  if (!isTable(envRaw)) {
    throw new ConfigError(`[servers.${name}].env must be a table`);
  }
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(envRaw)) {
    env[key] = expandVars(pyStr(value), environ);
  }
  const cwd = entry["cwd"];
  if (cwd !== undefined && typeof cwd !== "string") {
    throw new ConfigError(`[servers.${name}].cwd must be a string`);
  }
  return {
    name,
    command: expandVars(command, environ),
    args: args.map((arg) => expandVars(arg, environ)),
    env,
    cwd: cwd ?? null,
  };
}

function limitValue(name: string, value: unknown): unknown {
  if ((INTEGER_LIMITS as readonly string[]).includes(name)) {
    if (typeof value !== "bigint") {
      throw new ConfigError(`[limits].${name} must be an integer, got ${typeName(value)}: ${repr(value)}`);
    }
    return clamp(value);
  }
  if (name === "query_timeout_seconds") {
    if (typeof value !== "bigint" && typeof value !== "number") {
      throw new ConfigError(`[limits].${name} must be a number, got ${typeName(value)}: ${repr(value)}`);
    }
    return Number(value);
  }
  if (name === "duckdb_max_memory" && typeof value !== "string") {
    throw new ConfigError(`[limits].${name} must be a string, got ${typeName(value)}`);
  }
  return value;
}

interface ParsedLimits {
  readonly limits: Limits;
  readonly sessionBytes: bigint;
}

function rawInteger(table: Table, name: keyof Limits): bigint {
  const value = table[name];
  return typeof value === "bigint" ? value : BigInt(DEFAULT_LIMITS[name] as number);
}

function parseLimits(raw: unknown): ParsedLimits {
  const table = raw ?? {};
  if (!isTable(table)) {
    throw new ConfigError("[limits] must be a table");
  }
  const unknown = Object.keys(table).filter((key) => !Object.hasOwn(DEFAULT_LIMITS, key));
  if (unknown.length > 0) {
    throw new ConfigError(`[limits] has unknown keys: ${unknown.sort().join(", ")}`);
  }
  const ordered = [...INTEGER_LIMITS, "query_timeout_seconds", "duckdb_max_memory"].filter((key) => key in table);
  const overrides: Record<string, unknown> = {};
  for (const key of ordered) {
    overrides[key] = limitValue(key, table[key]);
  }
  const sessionBytes = rawInteger(table, "max_session_bytes");
  const payloadBytes = rawInteger(table, "max_payload_bytes");
  let limits: Limits;
  try {
    limits = makeLimits(overrides);
  } catch (error) {
    if (error instanceof LimitsError) {
      throw new ConfigError(error.message);
    }
    throw error;
  }
  if (sessionBytes < payloadBytes) {
    throw new ConfigError(
      `[limits].max_session_bytes must be at least max_payload_bytes (${payloadBytes}), got ${sessionBytes}`,
    );
  }
  return { limits, sessionBytes };
}

function paginationEntry(tool: string, entry: Table, limits: Limits, sessionBytes: bigint = BigInt(limits.max_session_bytes)): PaginationConfig {
  if (tool.trim() === "") {
    throw new ConfigError("[pagination] entries must be keyed by a non-empty tool name");
  }
  const where = `[pagination.${tool}]`;
  const merged: Record<string, unknown> = { ...PAGINATION_DEFAULTS, ...entry };
  for (const name of PAGINATION_NAMES) {
    const value = merged[name];
    if (typeof value !== "string" || value.trim() === "") {
      throw new ConfigError(`${where}.${name} must be a non-empty string`);
    }
  }
  if (merged["limit_arg"] === merged["offset_arg"]) {
    throw new ConfigError(`${where}.limit_arg and offset_arg must differ`);
  }
  for (const name of PAGINATION_COUNTS) {
    const value = merged[name];
    if (typeof value === "number" && Object.hasOwn(entry, name)) {
      throw new ConfigError(`${where}.${name} must be an integer, got ${typeName(value)}: ${repr(value)}`);
    }
    if (typeof value !== "bigint" && typeof value !== "number") {
      throw new ConfigError(`${where}.${name} must be an integer, got ${typeName(value)}: ${repr(value)}`);
    }
    if (value < 1) {
      throw new ConfigError(`${where}.${name} must be at least 1, got ${String(value)}`);
    }
    merged[name] = typeof value === "bigint" ? clamp(value) : value;
  }
  const rawMaxBytes = typeof entry["max_bytes"] === "bigint" ? entry["max_bytes"] : BigInt(PAGINATION_DEFAULTS.max_bytes);
  const seconds = merged["max_seconds"];
  if (typeof seconds !== "bigint" && typeof seconds !== "number") {
    throw new ConfigError(`${where}.max_seconds must be a number, got ${typeName(seconds)}: ${repr(seconds)}`);
  }
  const secondsValue = Number(seconds);
  if (!Number.isFinite(secondsValue) || secondsValue <= 0) {
    throw new ConfigError(`${where}.max_seconds must be a positive finite number, got ${repr(secondsValue)}`);
  }
  merged["max_seconds"] = secondsValue;
  const config = { ...merged, tool } as unknown as PaginationConfig;
  if (rawMaxBytes > sessionBytes) {
    throw new ConfigError(
      `${where}.max_bytes (${rawMaxBytes}) exceeds [limits].max_session_bytes (${sessionBytes}); ` +
        "a collection that cannot be retained would never get a table",
    );
  }
  return Object.freeze(config);
}

function parsePagination(raw: unknown, { limits, sessionBytes }: ParsedLimits): Map<string, PaginationConfig> {
  const table = raw ?? {};
  if (!isTable(table)) {
    throw new ConfigError("[pagination] must be a table of [pagination.<tool>] tables");
  }
  const known = new Set(Object.keys(PAGINATION_DEFAULTS));
  const parsed = new Map<string, PaginationConfig>();
  for (const [tool, entry] of Object.entries(table)) {
    if (!isTable(entry)) {
      throw new ConfigError(`[pagination.${tool}] must be a table`);
    }
    const unknown = Object.keys(entry).filter((key) => !known.has(key));
    if (unknown.length > 0) {
      throw new ConfigError(`[pagination.${tool}] has unknown keys: ${unknown.sort().join(", ")}`);
    }
    parsed.set(tool, paginationEntry(tool, entry, limits, sessionBytes));
  }
  return parsed;
}

export function parseConfig(raw: Table, environ: Environment = process.env): Config {
  const server = parseServer(raw, environ);
  const parsed = parseLimits(raw["limits"]);
  const pagination = parsePagination(raw["pagination"], parsed);
  return { server, limits: parsed.limits, pagination };
}

export function paginationConfig(tool: string, overrides: Readonly<Record<string, unknown>> = {}, limits: Limits = DEFAULT_LIMITS): PaginationConfig {
  const entry: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(overrides)) {
    entry[key] = typeof value === "number" && Number.isInteger(value) && key !== "max_seconds" ? BigInt(value) : value;
  }
  return paginationEntry(tool, entry, limits);
}
