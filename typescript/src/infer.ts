import { dumpJson, isJsonArray, isJsonObject, type JsonValue } from "./json.ts";

export const INT64_MIN = -(2n ** 63n);
export const INT64_MAX = 2n ** 63n - 1n;
export const INT128_MIN = -(2n ** 127n);
export const INT128_MAX = 2n ** 127n - 1n;
export const EXACT_FLOAT_MAX = 2n ** 53n;

export const ColumnType = {
  BOOLEAN: "BOOLEAN",
  BIGINT: "BIGINT",
  HUGEINT: "HUGEINT",
  DOUBLE: "DOUBLE",
  VARCHAR: "VARCHAR",
  JSON: "JSON",
} as const;

export type ColumnType = (typeof ColumnType)[keyof typeof ColumnType];

export interface Inference {
  readonly type: ColumnType;
  readonly exact: boolean;
}

export interface ColumnSpec extends Inference {
  readonly name: string;
  readonly renamedFrom: string | null;
}

export type SqlValue = null | boolean | bigint | number | string;

function within(value: bigint, min: bigint, max: bigint): boolean {
  return value >= min && value <= max;
}

function magnitude(value: bigint): bigint {
  return value < 0n ? -value : value;
}

export function inferColumn(values: readonly JsonValue[]): Inference {
  const present = values.filter((value) => value !== null);
  if (present.length === 0) {
    return { type: ColumnType.VARCHAR, exact: true };
  }
  if (present.some((value) => isJsonObject(value) || isJsonArray(value))) {
    return { type: ColumnType.JSON, exact: false };
  }
  if (present.every((value) => typeof value === "boolean")) {
    return { type: ColumnType.BOOLEAN, exact: true };
  }
  if (present.every((value) => typeof value === "string")) {
    return { type: ColumnType.VARCHAR, exact: true };
  }
  const ints: bigint[] = [];
  const floats: number[] = [];
  for (const value of present) {
    if (typeof value === "bigint") {
      ints.push(value);
    } else if (typeof value === "number") {
      floats.push(value);
    } else {
      return { type: ColumnType.VARCHAR, exact: false };
    }
  }
  if (floats.length === 0) {
    if (ints.every((value) => within(value, INT64_MIN, INT64_MAX))) {
      return { type: ColumnType.BIGINT, exact: ints.every((value) => magnitude(value) <= EXACT_FLOAT_MAX) };
    }
    if (ints.every((value) => within(value, INT128_MIN, INT128_MAX))) {
      return { type: ColumnType.HUGEINT, exact: false };
    }
    return { type: ColumnType.VARCHAR, exact: false };
  }
  return { type: ColumnType.DOUBLE, exact: false };
}

function toDouble(value: JsonValue): number {
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "bigint") {
    const converted = Number(value);
    if (!Number.isFinite(converted)) {
      throw new RangeError("integer too large to convert to DOUBLE");
    }
    return converted;
  }
  throw new TypeError(`cannot store ${typeof value} in a DOUBLE column`);
}

function toInteger(value: JsonValue): bigint {
  if (typeof value === "bigint") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new RangeError(`cannot store ${value} in an integer column`);
    }
    return BigInt(Math.trunc(value));
  }
  throw new TypeError(`cannot store ${typeof value} in an integer column`);
}

export function coerce(value: JsonValue, type: ColumnType): SqlValue {
  if (value === null) {
    return null;
  }
  switch (type) {
    case ColumnType.JSON:
      return dumpJson(value);
    case ColumnType.VARCHAR:
      if (typeof value === "string") {
        return value;
      }
      if (typeof value === "boolean") {
        return value ? "true" : "false";
      }
      return dumpJson(value);
    case ColumnType.BOOLEAN:
      if (typeof value !== "boolean") {
        throw new TypeError(`cannot store ${typeof value} in a BOOLEAN column`);
      }
      return value;
    case ColumnType.DOUBLE:
      return toDouble(value);
    case ColumnType.BIGINT:
    case ColumnType.HUGEINT:
      return toInteger(value);
  }
}
