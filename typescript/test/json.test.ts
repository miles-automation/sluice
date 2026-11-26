import assert from "node:assert/strict";
import { test } from "node:test";
import fc from "fast-check";
import { dumpJson, isJsonArray, JsonParseError, MAX_INT_DIGITS, parseJson, pythonFloatRepr, type JsonValue } from "../src/json.ts";
import { coerce, ColumnType } from "../src/infer.ts";
import { float, integer, json, key, objectOf, scalar } from "./arbitraries.ts";

function plain(value: JsonValue): unknown {
  if (typeof value === "bigint") {
    return Number(value);
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  if (isJsonArray(value)) {
    return value.map(plain);
  }
  return Object.fromEntries([...value].map(([name, item]) => [name, plain(item)]));
}

function standard(value: JsonValue): boolean {
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (typeof value === "bigint") {
    return Number.isSafeInteger(Number(value)) && BigInt(Number(value)) === value;
  }
  if (value === null || typeof value !== "object") {
    return true;
  }
  return isJsonArray(value) ? value.every(standard) : [...value.values()].every(standard);
}

test("integers and floats stay distinct on the way in", () => {
  assert.equal(parseJson("1"), 1n);
  assert.equal(parseJson("1.0"), 1);
  assert.equal(parseJson("1e2"), 100);
  assert.equal(parseJson("9007199254740993"), 9007199254740993n);
  assert.equal(parseJson("170141183460469231731687303715884105727"), 2n ** 127n - 1n);
  assert.ok(Object.is(parseJson("-0.0"), -0));
  assert.equal(parseJson("-0"), 0n);
});

test("the text channel accepts the literals Python's json module accepts", () => {
  assert.deepEqual(parseJson("[NaN, Infinity, -Infinity, 1e999]"), [Number.NaN, Infinity, -Infinity, Infinity]);
  for (const bad of ["nan", "-Inf", "+1", "01", "1.", ".5", "[1,]", '{"a":1,}', "﻿1", '"\u0001"', " 1", "1 2", ""]) {
    assert.throws(() => parseJson(bad), JsonParseError, bad);
  }
});

test("object keys keep payload order and the last duplicate wins", () => {
  const parsed = parseJson('{"b": 1, "1": 2, "a": 3, "b": 4}');
  assert.ok(parsed instanceof Map);
  assert.deepEqual([...parsed], [["b", 4n], ["1", 2n], ["a", 3n]]);
  const proto = parseJson('{"__proto__": 1}');
  assert.ok(proto instanceof Map);
  assert.equal(proto.get("__proto__"), 1n);
});

test("integer literals beyond Python's digit limit are not JSON", () => {
  assert.equal(parseJson("9".repeat(MAX_INT_DIGITS)), BigInt("9".repeat(MAX_INT_DIGITS)));
  assert.equal(parseJson(`-${"9".repeat(MAX_INT_DIGITS)}`), -BigInt("9".repeat(MAX_INT_DIGITS)));
  assert.throws(() => parseJson("9".repeat(MAX_INT_DIGITS + 1)), JsonParseError);
});

test("dumpJson writes what Python's json.dumps writes", () => {
  const value = parseJson('{"b": 1, "1": [1.0, -0, -0.0, 1e400, "\\u00e9\\ud83d\\ude00\\u007f\\n\\"", "\\ud800x"], "a": {"x": null, "x": true}, "big": 123456789012345678901234567890}');
  assert.equal(
    dumpJson(value),
    '{"b": 1, "1": [1.0, 0, -0.0, Infinity, "\\u00e9\\ud83d\\ude00\\u007f\\n\\"", "\\ud800x"], "a": {"x": true}, "big": 123456789012345678901234567890}',
  );
  assert.equal(dumpJson([new Map(), []]), "[{}, []]");
  assert.equal(dumpJson([Number.NaN, -Infinity]), "[NaN, -Infinity]");
});

test("floats print as Python's repr", () => {
  const vectors: [number, string][] = [
    [1e16, "1e+16"],
    [1e15, "1000000000000000.0"],
    [1e-5, "1e-05"],
    [1e-4, "0.0001"],
    [0.1 + 0.2, "0.30000000000000004"],
    [1.5e300, "1.5e+300"],
    [5e-324, "5e-324"],
    [123456789012345678, "1.2345678901234568e+17"],
    [-2.5, "-2.5"],
    [100, "100.0"],
  ];
  for (const [value, expected] of vectors) {
    assert.equal(pythonFloatRepr(value), expected);
  }
});

test("nesting 100,000 deep parses, dumps and coerces like Python", () => {
  const depth = 100_000;
  const arrays = `${"[".repeat(depth)}${"]".repeat(depth)}`;
  const parsedArrays = parseJson(arrays);
  assert.equal(dumpJson(parsedArrays), arrays);
  assert.equal(coerce(parsedArrays, ColumnType.JSON), arrays);
  const objects = `${'{"a": '.repeat(depth)}1${"}".repeat(depth)}`;
  const parsedObjects = parseJson(objects.replaceAll(" ", ""));
  assert.equal(dumpJson(parsedObjects), objects);
  assert.equal(coerce(parsedObjects, ColumnType.JSON), objects);
  const mixed = `${'[{"k": '.repeat(depth)}null${"}]".repeat(depth)}`;
  assert.equal(dumpJson(parseJson(mixed)), mixed);
});

test("nesting far past Python's recursion limit still parses without a stack overflow", () => {
  const depth = 1_000_000;
  assert.equal(dumpJson(parseJson(`${"[".repeat(depth)}${"]".repeat(depth)}`)).length, depth * 2);
  assert.throws(() => parseJson(`${"[".repeat(depth)}${"]".repeat(depth - 1)}`), JsonParseError);
});

test("an escaped surrogate never pairs with a raw one", () => {
  assert.equal(parseJson('"\\ud83d\\ude00"'), "\u{1F600}");
  assert.equal(parseJson('"\ud83d\ude00"'), "\u{1F600}");
  assert.equal(parseJson('"\\ud83dA\\ud83d"'), "\ud83dA\ud83d");
  assert.equal(parseJson('"\ud83d\\u0041"'), "\ud83dA");
  for (const text of ['{"\ud83d\\ude00": 1, "\u{1F600}": 2}', '"\\ud83d\ude00"', '"x\ud83d\\ude00"']) {
    assert.throws(() => parseJson(text), JsonParseError, text);
  }
});

test("property: dump then parse is the identity on the lossless model", () => {
  fc.assert(
    fc.property(json, (value) => {
      const dumped = dumpJson(value);
      assert.equal(dumpJson(parseJson(dumped)), dumped);
    }),
    { numRuns: 500 },
  );
});

test("property: on standard JSON the parser agrees with JSON.parse", () => {
  fc.assert(
    fc.property(json.filter(standard), (value) => {
      assert.deepEqual(plain(parseJson(dumpJson(value))), JSON.parse(dumpJson(value)));
    }),
    { numRuns: 500 },
  );
});

test("property: integers of any size survive and stay integers", () => {
  fc.assert(
    fc.property(fc.oneof(integer, fc.bigInt({ min: -(10n ** 300n), max: 10n ** 300n })), (value) => {
      assert.equal(parseJson(value.toString()), value);
    }),
  );
});

test("property: a float repr round-trips and never reads back as an integer", () => {
  fc.assert(
    fc.property(float.filter(Number.isFinite), (value) => {
      const repr = pythonFloatRepr(value);
      assert.ok(Object.is(Number(repr), value));
      assert.ok(Object.is(parseJson(repr), value));
    }),
    { numRuns: 2000 },
  );
});

test("property: object key order survives a round trip", () => {
  fc.assert(
    fc.property(objectOf(scalar, 10), (value) => {
      const parsed = parseJson(dumpJson(value));
      assert.ok(parsed instanceof Map);
      assert.deepEqual([...parsed.keys()], [...value.keys()]);
    }),
  );
});

test("property: dumped text is ASCII", () => {
  fc.assert(
    fc.property(key, (name) => {
      assert.match(dumpJson(name), /^[\x20-\x7e]*$/u);
    }),
  );
});
