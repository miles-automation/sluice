import type { Json } from "./fixtures.ts";

const INTEGER = /^-?[0-9]+$/;

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
    return text.toLowerCase() === String(bool);
  }
  if (text.trim() === "" || Number.isNaN(Number(text))) {
    return false;
  }
  const double = field(expected, "double");
  if (double !== undefined) {
    return Number(text) === Number(double);
  }
  return approx(expected, Number(text));
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
  if (typeof value !== "number") {
    return false;
  }
  const double = field(expected, "double");
  if (double !== undefined) {
    return value === Number(double);
  }
  return approx(expected, value);
}
