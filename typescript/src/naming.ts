import { blake2bHex } from "./digest.ts";

export const MAX_MOUNTED_NAME = 128;
export const SLUG_MAX = 40;
export const TAG_LEN = 12;
export const COLLECTION_SUFFIX = "all";

const NON_ALNUM = /[^a-z0-9]+/gu;
const EDGE_UNDERSCORES = /^_+|_+$/gu;

export class NameTooLongError extends Error {
  override readonly name = "NameTooLongError";
}

export class NameCollisionError extends Error {
  override readonly name = "NameCollisionError";
}

export type ToolPair = readonly [server: string, tool: string];

export function slug(value: string): string {
  const collapsed = value.toLowerCase().replace(NON_ALNUM, "_").replace(EDGE_UNDERSCORES, "");
  return collapsed.slice(0, SLUG_MAX) || "x";
}

export function tag(value: string): string {
  return blake2bHex(value, 8).slice(0, TAG_LEN);
}

export function identity(server: string, tool: string): string {
  return `${server}\u0000${tool}`;
}

function bounded(name: string, kind: string, server: string, tool: string, limit: number): string {
  if (name.length > limit) {
    throw new NameTooLongError(`${kind} name for ${server}/${tool} is ${name.length} chars, over the ${limit} limit`);
  }
  return name;
}

export function mountedName(server: string, tool: string, limit: number = MAX_MOUNTED_NAME): string {
  const name = `${slug(server)}__${slug(tool)}__${tag(identity(server, tool))}`;
  return bounded(name, "mounted", server, tool, limit);
}

export function collectionName(server: string, tool: string, limit: number = MAX_MOUNTED_NAME): string {
  const digest = tag(`${identity(server, tool)}\u0000${COLLECTION_SUFFIX}`);
  const name = `${slug(server)}__${slug(tool)}__${COLLECTION_SUFFIX}__${digest}`;
  return bounded(name, "collection", server, tool, limit);
}

export function tableName(mounted: string, scopeTag: string, seq: number): string {
  if (!Number.isSafeInteger(seq)) {
    throw new RangeError(`table sequence must be an integer, got ${seq}`);
  }
  const padded = seq < 0 ? `-${String(-seq).padStart(3, "0")}` : String(seq).padStart(4, "0");
  return `${mounted}__${scopeTag}__${padded}`;
}

export function quoteIdent(name: string): string {
  return `"${name.replaceAll('"', '""')}"`;
}

export function assertInjective(
  pairs: Iterable<ToolPair>,
  mount: (server: string, tool: string) => string = mountedName,
): Map<string, ToolPair> {
  const seen = new Map<string, ToolPair>();
  for (const [server, tool] of pairs) {
    const name = mount(server, tool);
    const clash = seen.get(name);
    if (clash !== undefined && (clash[0] !== server || clash[1] !== tool)) {
      throw new NameCollisionError(
        `${clash[0]}/${JSON.stringify(clash[1])} and ${server}/${JSON.stringify(tool)} both mount as ${JSON.stringify(name)}`,
      );
    }
    seen.set(name, [server, tool]);
  }
  return seen;
}
