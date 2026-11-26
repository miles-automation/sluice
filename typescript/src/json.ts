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
    const char = this.#text[this.#pos];
    switch (char) {
      case '"':
        return this.#string();
      case "{":
        return this.#object();
      case "[":
        return this.#array();
      case undefined:
        throw new JsonParseError("Expecting value", this.#pos);
      default: {
        const literal = this.#literal();
        return literal === undefined ? this.#number() : literal[0];
      }
    }
  }

  #literal(): readonly [JsonValue] | undefined {
    for (const [word, value] of LITERALS) {
      if (this.#text.startsWith(word, this.#pos)) {
        this.#pos += word.length;
        return [value];
      }
    }
    return undefined;
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
          out += String.fromCharCode(Number.parseInt(hex, 16));
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

  #object(): JsonObject {
    const object = new Map<string, JsonValue>();
    this.#pos += 1;
    this.#skipWhitespace();
    if (this.#text[this.#pos] === "}") {
      this.#pos += 1;
      return object;
    }
    for (;;) {
      if (this.#text[this.#pos] !== '"') {
        throw new JsonParseError("Expecting property name enclosed in double quotes", this.#pos);
      }
      const key = this.#string();
      this.#skipWhitespace();
      this.#expect(":", "Expecting ':' delimiter");
      this.#skipWhitespace();
      object.set(key, this.#value());
      this.#skipWhitespace();
      if (this.#text[this.#pos] === "}") {
        this.#pos += 1;
        return object;
      }
      this.#expect(",", "Expecting ',' delimiter");
      this.#skipWhitespace();
    }
  }

  #array(): JsonArray {
    const array: JsonValue[] = [];
    this.#pos += 1;
    this.#skipWhitespace();
    if (this.#text[this.#pos] === "]") {
      this.#pos += 1;
      return array;
    }
    for (;;) {
      array.push(this.#value());
      this.#skipWhitespace();
      if (this.#text[this.#pos] === "]") {
        this.#pos += 1;
        return array;
      }
      this.#expect(",", "Expecting ',' delimiter");
      this.#skipWhitespace();
    }
  }
}

export function parseJson(text: string): JsonValue {
  return new Parser(text).document();
}

const NEEDS_ESCAPE = /["\\\u0000-\u001f\u007f-￿]/g;
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

export function dumpJson(value: JsonValue): string {
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
  if (isJsonArray(value)) {
    return `[${value.map(dumpJson).join(", ")}]`;
  }
  const entries: string[] = [];
  for (const [key, item] of value) {
    entries.push(`${quote(key)}: ${dumpJson(item)}`);
  }
  return `{${entries.join(", ")}}`;
}
