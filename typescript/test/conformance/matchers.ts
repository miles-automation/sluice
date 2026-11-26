import type { Json } from "./fixtures.ts";

const INTEGER = /^-?[0-9]+$/;
const NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$/;
const DECIMAL = /^-?[0-9]+(?:\.[0-9]+)?$/;
const BOOLEAN_TEXT = new Set(["true", "false", "True", "False"]);

function parseNumber(text: string): number | undefined {
  return NUMBER.test(text) ? Number(text) : undefined;
}

function expectedDouble(value: Json | undefined): number {
  if (typeof value === "number") {
    return value;
  }
  const parsed = typeof value === "string" ? parseNumber(value) : undefined;
  if (parsed === undefined) {
    throw new TypeError(`not a numeric literal: ${JSON.stringify(value)}`);
  }
  return parsed;
}

function isDecimalValue(value: unknown): value is { readonly scale: number; readonly width: number } {
  return typeof value === "object" && value !== null && "scale" in value && "width" in value;
}

function field(matcher: { [key: string]: Json }, name: string): Json | undefined {
  return Object.hasOwn(matcher, name) ? matcher[name] : undefined;
}

function isClose(actual: number, target: number, rel: number, abs: number): boolean {
  if (actual === target) {
    return true;
  }
  if (!Number.isFinite(actual) || !Number.isFinite(target)) {
    return false;
  }
  const diff = Math.abs(actual - target);
  return diff <= Math.max(rel * Math.max(Math.abs(actual), Math.abs(target)), abs);
}

function approx(matcher: { [key: string]: Json }, actual: number): boolean {
  const target = Number(field(matcher, "approx"));
  if (field(matcher, "exact") === false && actual === target) {
    return false;
  }
  return isClose(actual, target, Number(field(matcher, "rel")), Number(field(matcher, "abs")));
}

export function matchCell(expected: Json, text: string): boolean {
  if (expected === null) {
    return text === "NULL";
  }
  if (typeof expected === "string") {
    return text === expected;
  }
  if (typeof expected === "number") {
    return INTEGER.test(text) && BigInt(text) === BigInt(expected);
  }
  if (typeof expected !== "object" || Array.isArray(expected)) {
    throw new TypeError(`not a cell matcher: ${JSON.stringify(expected)}`);
  }
  const integer = field(expected, "int");
  if (typeof integer === "string") {
    return INTEGER.test(text) && BigInt(text) === BigInt(integer);
  }
  const bool = field(expected, "bool");
  if (typeof bool === "boolean") {
    return BOOLEAN_TEXT.has(text) && text.toLowerCase() === String(bool);
  }
  const decimal = field(expected, "decimal");
  if (typeof decimal === "string") {
    return DECIMAL.test(text) && text === decimal;
  }
  const actual = parseNumber(text);
  if (actual === undefined) {
    return false;
  }
  const double = field(expected, "double");
  if (double !== undefined) {
    return actual === expectedDouble(double);
  }
  return approx(expected, actual);
}

export function matchValue(expected: Json, value: unknown): boolean {
  if (expected === null) {
    return value === null;
  }
  if (typeof expected === "string") {
    return typeof value === "string" && value === expected;
  }
  if (typeof expected === "number") {
    return typeof value === "bigint" && value === BigInt(expected);
  }
  if (typeof expected !== "object" || Array.isArray(expected)) {
    throw new TypeError(`not a value matcher: ${JSON.stringify(expected)}`);
  }
  const integer = field(expected, "int");
  if (typeof integer === "string") {
    return typeof value === "bigint" && value === BigInt(integer);
  }
  const bool = field(expected, "bool");
  if (typeof bool === "boolean") {
    return value === bool;
  }
  const decimal = field(expected, "decimal");
  if (typeof decimal === "string") {
    return isDecimalValue(value) && String(value) === decimal;
  }
  if (typeof value !== "number") {
    return false;
  }
  const double = field(expected, "double");
  if (double !== undefined) {
    return value === expectedDouble(double);
  }
  return approx(expected, value);
}
