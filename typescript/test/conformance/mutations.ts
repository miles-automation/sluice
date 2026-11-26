import type { Json } from "./fixtures.ts";

export type AstPath = readonly (string | number)[];

type JsonObject = { [key: string]: Json };

function isObject(value: Json | undefined): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function resolve(document: Json, path: AstPath): Json {
  let node: Json = document;
  for (const step of path) {
    const next: Json | undefined = Array.isArray(node)
      ? node[Number(step)]
      : isObject(node)
        ? node[String(step)]
        : undefined;
    if (next === undefined) {
      throw new Error(`path ${JSON.stringify(path)} does not resolve`);
    }
    node = next;
  }
  return node;
}

function container(document: Json, path: AstPath): { target: JsonObject | Json[]; last: string | number } {
  const parent = resolve(document, path.slice(0, -1));
  const last = path.at(-1);
  if (last === undefined || (!isObject(parent) && !Array.isArray(parent))) {
    throw new Error(`path ${JSON.stringify(path)} has no container`);
  }
  return { target: parent, last };
}

function assign(document: Json, path: AstPath, value: Json): void {
  const { target, last } = container(document, path);
  if (Array.isArray(target)) {
    target[Number(last)] = value;
  } else {
    target[String(last)] = value;
  }
}

function find(node: Json, wanted: string): JsonObject | undefined {
  if (isObject(node) && node["type"] === wanted) {
    return node;
  }
  const children = Array.isArray(node) ? node : isObject(node) ? Object.values(node) : [];
  for (const child of children) {
    const found = find(child, wanted);
    if (found !== undefined) {
      return found;
    }
  }
  return undefined;
}

function substitute(template: Json, node: Json): Json {
  if (template === "$node") {
    return structuredClone(node);
  }
  if (Array.isArray(template)) {
    return template.map((value) => substitute(value, node));
  }
  if (isObject(template)) {
    return Object.fromEntries(Object.entries(template).map(([key, value]) => [key, substitute(value, node)]));
  }
  return template;
}

function pathOf(value: Json | undefined): AstPath {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((step) => (typeof step === "number" ? step : String(step)));
}

export function applyMutations(sql: string, mutations: readonly Json[], serialize: (sql: string) => Json): Json {
  const [first, ...rest] = mutations;
  const replaces = isObject(first) && first["op"] === "replace_serialized";
  const mutated = structuredClone(serialize(replaces && isObject(first) ? String(first["sql"]) : sql));
  for (const mutation of replaces ? rest : mutations) {
    if (!isObject(mutation)) {
      throw new Error("a mutation must be an object");
    }
    const path = pathOf(mutation["path"]);
    switch (mutation["op"]) {
      case "update": {
        const scope = resolve(mutated, pathOf(mutation["within"]));
        const target = find(scope, String(mutation["find"]));
        const changes = mutation["set"];
        if (target === undefined || !isObject(changes)) {
          throw new Error(`no ${String(mutation["find"])} node to update`);
        }
        Object.assign(target, changes);
        break;
      }
      case "set":
        assign(mutated, path, mutation["value"] ?? null);
        break;
      case "delete": {
        const { target, last } = container(mutated, path);
        if (Array.isArray(target)) {
          target.splice(Number(last), 1);
        } else {
          delete target[String(last)];
        }
        break;
      }
      case "rename_first": {
        const target = resolve(mutated, path);
        const keys = mutation["keys"];
        if (!isObject(target) || !Array.isArray(keys)) {
          throw new Error("rename_first needs an object and a key list");
        }
        const key = keys.map(String).find((candidate) => Object.hasOwn(target, candidate));
        if (key === undefined) {
          throw new Error(`none of ${JSON.stringify(keys)} is present`);
        }
        target[String(mutation["to"])] = target[key] ?? null;
        delete target[key];
        break;
      }
      case "set_serialized": {
        const source = resolve(serialize(String(mutation["sql"])), ["statements", 0, "node"]);
        assign(mutated, path, substitute(mutation["value"] ?? null, source));
        break;
      }
      default:
        throw new Error(`mutation ${String(mutation["op"])} is unknown or out of place`);
    }
  }
  return mutated;
}
