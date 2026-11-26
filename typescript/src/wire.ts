import { isJsonArray, isJsonObject, pythonFloatRepr, type JsonValue } from "./json.ts";

declare global {
  interface JSON {
    rawJSON(text: string): unknown;
  }
}

export function rawJson(text: string): unknown {
  return JSON.rawJSON(text);
}

type Frame = readonly [JsonValue, (converted: unknown) => void];

function scalar(value: null | boolean | bigint | number | string): unknown {
  if (typeof value === "bigint") {
    return Number.isSafeInteger(Number(value)) ? Number(value) : rawJson(value.toString());
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    const text = pythonFloatRepr(value);
    return text === String(value) ? value : rawJson(text);
  }
  return value;
}

function define(object: Record<string, unknown>, key: string, value: unknown): void {
  Object.defineProperty(object, key, { value, enumerable: true, writable: true, configurable: true });
}

export function toWire(value: JsonValue): unknown {
  let root: unknown = null;
  const stack: Frame[] = [[value, (converted) => (root = converted)]];
  for (let frame = stack.pop(); frame !== undefined; frame = stack.pop()) {
    const [item, assign] = frame;
    if (isJsonArray(item)) {
      const array: unknown[] = new Array<unknown>(item.length).fill(null);
      assign(array);
      item.forEach((child, index) => stack.push([child, (converted) => (array[index] = converted)]));
    } else if (isJsonObject(item)) {
      const object: Record<string, unknown> = {};
      assign(object);
      for (const [key, child] of item) {
        define(object, key, null);
        stack.push([child, (converted) => define(object, key, converted)]);
      }
    } else {
      assign(scalar(item));
    }
  }
  return root;
}

export function toPlainObject(value: JsonValue): Record<string, unknown> {
  const converted = toWire(value);
  return typeof converted === "object" && converted !== null && !Array.isArray(converted)
    ? (converted as Record<string, unknown>)
    : {};
}
