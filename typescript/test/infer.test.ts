import assert from "node:assert/strict";
import { test } from "node:test";
import fc from "fast-check";
import { coerce, ColumnType, EXACT_FLOAT_MAX, INT64_MAX, INT64_MIN, inferColumn } from "../src/infer.ts";
import { parseJson, type JsonValue } from "../src/json.ts";
import { float, integer, json, scalar, text } from "./arbitraries.ts";

const column = fc.array(json, { maxLength: 8 });

test("coerce matches Python's coerce", () => {
  assert.equal(coerce(null, ColumnType.BIGINT), null);
  assert.equal(coerce(parseJson('{"a": 1}'), ColumnType.JSON), '{"a": 1}');
  assert.equal(coerce("x", ColumnType.VARCHAR), "x");
  assert.equal(coerce(1n, ColumnType.VARCHAR), "1");
  assert.equal(coerce(true, ColumnType.VARCHAR), "true");
  assert.equal(coerce(1n, ColumnType.DOUBLE), 1);
  assert.equal(coerce(1, ColumnType.BIGINT), 1n);
  assert.equal(coerce(1e16, ColumnType.VARCHAR), "1e+16");
  assert.equal(coerce(2.0, ColumnType.VARCHAR), "2.0");
  assert.equal(coerce(parseJson('["\\u00e9"]'), ColumnType.JSON), '["\\u00e9"]');
  assert.throws(() => coerce(10n ** 400n, ColumnType.DOUBLE), RangeError);
});

test("property: inference ignores order and nulls", () => {
  fc.assert(
    fc.property(column, fc.nat({ max: 4 }), (values, nulls) => {
      const expected = inferColumn(values);
      assert.deepEqual(inferColumn([...values].reverse()), expected);
      assert.deepEqual(inferColumn([...values, ...Array<JsonValue>(nulls).fill(null)]), expected);
    }),
    { numRuns: 500 },
  );
});

test("property: exact is claimed only inside its domain", () => {
  fc.assert(
    fc.property(column, (values) => {
      const { type, exact } = inferColumn(values);
      const present = values.filter((value) => value !== null);
      if (!exact) {
        return;
      }
      if (type === ColumnType.BIGINT) {
        for (const value of present) {
          assert.ok(typeof value === "bigint" && value >= -EXACT_FLOAT_MAX && value <= EXACT_FLOAT_MAX);
        }
      } else if (type === ColumnType.VARCHAR) {
        assert.ok(present.every((value) => typeof value === "string"));
      } else {
        assert.equal(type, ColumnType.BOOLEAN);
      }
    }),
    { numRuns: 1000 },
  );
});

test("property: any float makes a numeric column DOUBLE and inexact", () => {
  fc.assert(
    fc.property(fc.array(fc.oneof(integer, float)), float, (numbers, one) => {
      assert.deepEqual(inferColumn([...numbers, one]), { type: ColumnType.DOUBLE, exact: false });
    }),
  );
});

test("property: integer columns follow the int64 and 2^53 rules", () => {
  fc.assert(
    fc.property(fc.array(integer, { minLength: 1 }), (ints) => {
      const result = inferColumn(ints);
      if (ints.every((value) => value >= INT64_MIN && value <= INT64_MAX)) {
        const exact = ints.every((value) => value >= -EXACT_FLOAT_MAX && value <= EXACT_FLOAT_MAX);
        assert.deepEqual(result, { type: ColumnType.BIGINT, exact });
      } else {
        assert.equal(result.exact, false);
        assert.ok(result.type === ColumnType.HUGEINT || result.type === ColumnType.VARCHAR);
      }
    }),
  );
});

test("property: strings never become TIMESTAMP and mixed scalars never become JSON", () => {
  const stamp = fc.date({ noInvalidDate: true }).chain((date) =>
    fc.constantFrom(date.toISOString(), date.toISOString().slice(0, 10), date.toUTCString()),
  );
  fc.assert(
    fc.property(fc.array(fc.oneof(stamp, text), { minLength: 1 }), (strings) => {
      assert.deepEqual(inferColumn(strings), { type: ColumnType.VARCHAR, exact: true });
    }),
  );
  fc.assert(
    fc.property(fc.array(scalar), fc.oneof(stamp, text), fc.oneof(fc.boolean(), integer, float), (rest, word, other) => {
      assert.deepEqual(inferColumn([...rest, word, other]), { type: ColumnType.VARCHAR, exact: false });
    }),
  );
});

test("property: every value coerces into its inferred column", () => {
  fc.assert(
    fc.property(column, (values) => {
      const { type } = inferColumn(values);
      for (const value of values) {
        if (type === ColumnType.DOUBLE && typeof value === "bigint" && !Number.isFinite(Number(value))) {
          continue;
        }
        const stored = coerce(value, type);
        if (value === null) {
          assert.equal(stored, null);
        } else if (type === ColumnType.BIGINT || type === ColumnType.HUGEINT) {
          assert.equal(stored, value);
        } else if (type === ColumnType.VARCHAR || type === ColumnType.JSON) {
          assert.equal(typeof stored, "string");
        }
      }
    }),
    { numRuns: 1000 },
  );
});
