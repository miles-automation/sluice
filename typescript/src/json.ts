export type JsonArray = readonly JsonValue[];
export type JsonObject = ReadonlyMap<string, JsonValue>;
export type JsonValue = null | boolean | bigint | number | string | JsonArray | JsonObject;

export const MAX_INT_DIGITS = 4300;

export class JsonParseError extends Error {
  override readonly name = "JsonParseError";
  readonly position: number;

  constructor(message: string, position: number) {
    super(`${message} at position ${position}`);
    this.position = position;
  }
}

export function isJsonObject(value: JsonValue | undefined): value is JsonObject {
  return value instanceof Map;
}

export function isJsonArray(value: JsonValue | undefined): value is JsonArray {
  return Array.isArray(value);
}

const NUMBER = /(-?(?:0|[1-9][0-9]*))(\.[0-9]+)?([eE][-+]?[0-9]+)?/y;
const HEX4 = /^[0-9a-fA-F]{4}$/;
const SIMPLE_ESCAPES: Readonly<Record<string, string>> = {
  '"': '"',
  "\\": "\\",
  "/": "/",
  b: "\b",
  f: "\f",
  n: "\n",
  r: "\r",
  t: "\t",
};
const LITERALS: readonly (readonly [string, JsonValue])[] = [
  ["null", null],
  ["true", true],
  ["false", false],
  ["NaN", Number.NaN],
  ["Infinity", Number.POSITIVE_INFINITY],
  ["-Infinity", Number.NEGATIVE_INFINITY],
];

type Frame = { readonly items: JsonValue[] } | { readonly entries: Map<string, JsonValue>; key: string };

function isHighSurrogate(unit: number): boolean {
  return unit >= 0xd800 && unit <= 0xdbff;
}

function isLowSurrogate(unit: number): boolean {
  return unit >= 0xdc00 && unit <= 0xdfff;
}

class Parser {
  readonly #text: string;
  #pos = 0;

  constructor(text: string) {
    this.#text = text;
  }

  document(): JsonValue {
    this.#skipWhitespace();
    const value = this.#value();
    this.#skipWhitespace();
    if (this.#pos !== this.#text.length) {
      throw new JsonParseError("Extra data", this.#pos);
    }
    return value;
  }

  #skipWhitespace(): void {
    const text = this.#text;
    let pos = this.#pos;
    while (pos < text.length) {
      const code = text.charCodeAt(pos);
      if (code !== 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d) {
        break;
      }
      pos += 1;
    }
    this.#pos = pos;
  }

  #value(): JsonValue {
    const stack: Frame[] = [];
    for (;;) {
      let value: JsonValue;
      const char = this.#text[this.#pos];
      if (char === "[") {
        this.#pos += 1;
        this.#skipWhitespace();
        if (this.#text[this.#pos] !== "]") {
          stack.push({ items: [] });
          continue;
        }
        this.#pos += 1;
        value = [];
      } else if (char === "{") {
        this.#pos += 1;
        this.#skipWhitespace();
        if (this.#text[this.#pos] !== "}") {
          stack.push({ entries: new Map(), key: this.#key() });
          continue;
        }
        this.#pos += 1;
        value = new Map();
      } else {
        value = this.#scalar();
      }
      for (;;) {
        const frame = stack.at(-1);
        if (frame === undefined) {
          return value;
        }
        this.#skipWhitespace();
        const next = this.#text[this.#pos];
        if ("items" in frame) {
          frame.items.push(value);
          if (next === "]") {
            this.#pos += 1;
            stack.pop();
            value = frame.items;
            continue;
          }
          this.#expect(",", "Expecting ',' delimiter");
          this.#skipWhitespace();
          break;
        }
        frame.entries.set(frame.key, value);
        if (next === "}") {
          this.#pos += 1;
          stack.pop();
          value = frame.entries;
          continue;
        }
        this.#expect(",", "Expecting ',' delimiter");
        this.#skipWhitespace();
        frame.key = this.#key();
        break;
      }
    }
  }

  #key(): string {
    if (this.#text[this.#pos] !== '"') {
      throw new JsonParseError("Expecting property name enclosed in double quotes", this.#pos);
    }
    const key = this.#string();
    this.#skipWhitespace();
    this.#expect(":", "Expecting ':' delimiter");
    this.#skipWhitespace();
    return key;
  }

  #scalar(): JsonValue {
    const char = this.#text[this.#pos];
    if (char === '"') {
      return this.#string();
    }
    if (char === undefined) {
      throw new JsonParseError("Expecting value", this.#pos);
    }
    for (const [word, value] of LITERALS) {
      if (this.#text.startsWith(word, this.#pos)) {
        this.#pos += word.length;
        return value;
      }
    }
    return this.#number();
  }

  #number(): JsonValue {
    NUMBER.lastIndex = this.#pos;
    const match = NUMBER.exec(this.#text);
    if (match === null) {
      throw new JsonParseError("Expecting value", this.#pos);
    }
    const start = this.#pos;
    this.#pos = NUMBER.lastIndex;
    const [literal, integer, fraction, exponent] = match;
    if (fraction !== undefined || exponent !== undefined) {
      return Number(literal);
    }
    const digits = integer === undefined ? 0 : integer.length - (integer.startsWith("-") ? 1 : 0);
    if (digits > MAX_INT_DIGITS) {
      throw new JsonParseError(`Integer literal exceeds ${MAX_INT_DIGITS} digits`, start);
    }
    return BigInt(literal);
  }

  #string(): string {
    const text = this.#text;
    let pos = this.#pos + 1;
    let chunkStart = pos;
    let out = "";
    for (;;) {
      if (pos >= text.length) {
        throw new JsonParseError("Unterminated string", this.#pos);
      }
      const code = text.charCodeAt(pos);
      if (code === 0x22) {
        this.#pos = pos + 1;
        return out + text.slice(chunkStart, pos);
      }
      if (code === 0x5c) {
        out += text.slice(chunkStart, pos);
        const escape = text[pos + 1];
        if (escape === "u") {
          const hex = text.slice(pos + 2, pos + 6);
          if (!HEX4.test(hex)) {
            throw new JsonParseError("Invalid \\uXXXX escape", pos);
          }
          const unit = Number.parseInt(hex, 16);
          const rawBefore = chunkStart < pos ? text.charCodeAt(pos - 1) : -1;
          const rawAfter = pos + 6 < text.length ? text.charCodeAt(pos + 6) : -1;
          if ((isLowSurrogate(unit) && isHighSurrogate(rawBefore)) || (isHighSurrogate(unit) && isLowSurrogate(rawAfter))) {
            throw new JsonParseError("Escaped surrogate next to an unpaired raw surrogate", pos);
          }
          out += String.fromCharCode(unit);
          pos += 6;
        } else {
          const replacement = escape === undefined ? undefined : SIMPLE_ESCAPES[escape];
          if (replacement === undefined) {
            throw new JsonParseError("Invalid \\escape", pos);
          }
          out += replacement;
          pos += 2;
        }
        chunkStart = pos;
        continue;
      }
      if (code < 0x20) {
        throw new JsonParseError("Invalid control character", pos);
      }
      pos += 1;
    }
  }

  #expect(char: string, message: string): void {
    if (this.#text[this.#pos] !== char) {
      throw new JsonParseError(message, this.#pos);
    }
    this.#pos += 1;
  }
}

export function parseJson(text: string): JsonValue {
  return new Parser(text).document();
}

const NEEDS_ESCAPE = /["\\\u0000-\u001f\u007f-\uffff]/g;
const SHORT_ESCAPES: Readonly<Record<string, string>> = {
  '"': '\\"',
  "\\": "\\\\",
  "\n": "\\n",
  "\r": "\\r",
  "\t": "\\t",
  "\b": "\\b",
  "\f": "\\f",
};

function quote(text: string): string {
  const escaped = text.replace(
    NEEDS_ESCAPE,
    (char) => SHORT_ESCAPES[char] ?? `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  return `"${escaped}"`;
}

export function pythonFloatRepr(value: number): string {
  if (!Number.isFinite(value)) {
    throw new RangeError(`no float repr for ${value}`);
  }
  if (value === 0) {
    return Object.is(value, -0) ? "-0.0" : "0.0";
  }
  const [mantissa = "", power = "0"] = value.toExponential().split("e");
  const sign = mantissa.startsWith("-") ? "-" : "";
  const digits = mantissa.replace("-", "").replace(".", "");
  const exponent = Number(power);
  const point = exponent + 1;
  if (point > 16 || point < -3) {
    const body = digits.length > 1 ? `${digits.slice(0, 1)}.${digits.slice(1)}` : digits;
    const magnitude = String(Math.abs(exponent)).padStart(2, "0");
    return `${sign}${body}e${exponent < 0 ? "-" : "+"}${magnitude}`;
  }
  if (point <= 0) {
    return `${sign}0.${"0".repeat(-point)}${digits}`;
  }
  if (point >= digits.length) {
    return `${sign}${digits}${"0".repeat(point - digits.length)}.0`;
  }
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

function dumpNumber(value: number): string {
  if (Number.isNaN(value)) {
    return "NaN";
  }
  if (value === Number.POSITIVE_INFINITY) {
    return "Infinity";
  }
  if (value === Number.NEGATIVE_INFINITY) {
    return "-Infinity";
  }
  return pythonFloatRepr(value);
}

type DumpTask = { readonly raw: string } | { readonly value: JsonValue };

function dumpScalar(value: Exclude<JsonValue, JsonArray | JsonObject>): string {
  if (value === null) {
    return "null";
  }
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "bigint":
      return value.toString();
    case "number":
      return dumpNumber(value);
    case "string":
      return quote(value);
  }
}

export function dumpJson(value: JsonValue): string {
  const out: string[] = [];
  const tasks: DumpTask[] = [{ value }];
  for (let task = tasks.pop(); task !== undefined; task = tasks.pop()) {
    if ("raw" in task) {
      out.push(task.raw);
      continue;
    }
    const current = task.value;
    if (isJsonArray(current)) {
      tasks.push({ raw: "]" });
      for (let index = current.length - 1; index >= 0; index -= 1) {
        tasks.push({ value: current[index] ?? null });
        if (index > 0) {
          tasks.push({ raw: ", " });
        }
      }
      tasks.push({ raw: "[" });
    } else if (isJsonObject(current)) {
      const entries = [...current];
      tasks.push({ raw: "}" });
      for (let index = entries.length - 1; index >= 0; index -= 1) {
        const [key, item] = entries[index] ?? ["", null];
        tasks.push({ value: item });
        tasks.push({ raw: `${index > 0 ? ", " : ""}${quote(key)}: ` });
      }
      tasks.push({ raw: "{" });
    } else {
      out.push(dumpScalar(current));
    }
  }
  return out.join("");
}
