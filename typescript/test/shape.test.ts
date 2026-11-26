import assert from "node:assert/strict";
import { test } from "node:test";
import fc from "fast-check";
import { isJsonArray, isJsonObject, type JsonObject } from "../src/json.ts";
import { EXTRA_COLUMN, extract, project, RESERVED, ROW_COLUMN, VALUE_COLUMN } from "../src/shape.ts";
import { json, row, scalar } from "./arbitraries.ts";

const CATEGORIES = new Set(["empty", "mixed_elements", "scalar"]);

test("property: extraction yields row sets or a categorised reason, never both", () => {
  fc.assert(
    fc.property(json, (payload) => {
      const result = extract(payload);
      if (result.reason === null) {
        assert.ok(result.rowSets.length > 0);
        for (const rowSet of result.rowSets) {
          assert.ok(rowSet.rows.length > 0);
        }
      } else {
        assert.deepEqual(result.rowSets, []);
        assert.ok(CATEGORIES.has(result.reason.split(":", 1)[0] ?? ""), result.reason);
      }
    }),
    { numRuns: 1000 },
  );
});

test("property: a list of objects is the row set as is", () => {
  fc.assert(
    fc.property(fc.array(row, { minLength: 1, maxLength: 5 }), (rows) => {
      const result = extract(rows);
      assert.equal(result.reason, null);
      assert.deepEqual(result.rowSets, [{ sourcePath: "$", rows }]);
    }),
  );
});

test("property: a list of scalars becomes one value column", () => {
  fc.assert(
    fc.property(fc.array(scalar, { minLength: 1, maxLength: 8 }), (items) => {
      const result = extract(items);
      assert.equal(result.reason, null);
      assert.deepEqual(
        result.rowSets.flatMap((rowSet) => rowSet.rows.map((item) => [...item])),
        items.map((item) => [[VALUE_COLUMN, item]]),
      );
    }),
  );
});

test("property: every object-only list in an object is materialized, in payload order", () => {
  fc.assert(
    fc.property(json.filter(isJsonObject), (payload) => {
      const result = extract(payload);
      const candidates = [...payload]
        .filter(([, value]) => isJsonArray(value) && value.length > 0 && value.every(isJsonObject))
        .map(([name]) => `$.${name}`);
      if (candidates.length > 0) {
        assert.deepEqual(
          result.rowSets.map((rowSet) => rowSet.sourcePath),
          candidates,
        );
      } else if (result.reason === null) {
        assert.deepEqual(result.rowSets, [{ sourcePath: "$", rows: [payload] }]);
      }
    }),
    { numRuns: 1000 },
  );
});

function presence(rows: readonly JsonObject[], name: string): number {
  return rows.filter((item) => item.has(name)).length;
}

test("property: projection keeps every source value exactly once", () => {
  fc.assert(
    fc.property(fc.array(row, { maxLength: 6 }), fc.integer({ min: 0, max: 10 }), (rows, maxColumns) => {
      const projection = project(rows, maxColumns);
      const columns = projection.columns;
      assert.equal(new Set(columns).size, columns.length);
      assert.equal(projection.hasExtra, projection.extraKeys.length > 0);
      const stored = projection.hasExtra ? columns.slice(0, -1) : columns;
      if (projection.hasExtra) {
        assert.equal(columns.at(-1), EXTRA_COLUMN);
      }
      assert.ok(stored.length <= maxColumns);
      for (const name of stored) {
        assert.ok(!RESERVED.includes(name));
      }
      const keys = new Set(rows.flatMap((item) => [...item.keys()]));
      assert.equal(stored.length + projection.extraKeys.length, keys.size);
      const kept = [...keys].filter((name) => !projection.extraKeys.includes(name));
      for (const name of kept) {
        for (const extra of projection.extraKeys) {
          assert.ok(presence(rows, name) >= presence(rows, extra));
        }
        const renamed = projection.renamed.get(name);
        if (renamed !== undefined) {
          assert.match(renamed, /__src[0-9]*$/u);
        }
      }
      assert.deepEqual(
        stored,
        kept.map((name) => projection.renamed.get(name) ?? name),
      );
      projection.records.forEach((record, index) => {
        const source = rows[index];
        assert.ok(source !== undefined);
        assert.equal(record.get(ROW_COLUMN), BigInt(index));
        for (const name of kept) {
          assert.equal(record.get(projection.renamed.get(name) ?? name), source.get(name) ?? null);
        }
        if (projection.hasExtra) {
          const leftover = record.get(EXTRA_COLUMN);
          const expected = projection.extraKeys.filter((name) => source.has(name)).map((name) => [name, source.get(name)]);
          if (expected.length === 0) {
            assert.equal(leftover, null);
          } else {
            assert.ok(leftover instanceof Map);
            assert.deepEqual([...leftover], expected);
          }
        }
      });
    }),
    { numRuns: 500 },
  );
});

test("projection refuses a negative column cap", () => {
  assert.throws(() => project([], -1), RangeError);
});
