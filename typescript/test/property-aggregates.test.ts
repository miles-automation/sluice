import assert from "node:assert/strict";
import { test } from "node:test";
import fc from "fast-check";
import { Interceptor } from "../src/intercept.ts";
import { dumpJson, type JsonObject, type JsonValue } from "../src/json.ts";
import { makeLimits } from "../src/limits.ts";
import type { ToolResult } from "../src/models.ts";
import { QueryTool } from "../src/query.ts";
import { Store } from "../src/store.ts";

const SAFE = 2n ** 53n;
const safeInt = fc.bigInt({ min: -SAFE, max: SAFE });
const SMALLEST_NORMAL = 2.2250738585072014e-308;
const safeFloat = fc
  .double({ min: -1_000_000, max: 1_000_000, noNaN: true, noDefaultInfinity: true })
  .map((value) => (Math.abs(value) < SMALLEST_NORMAL ? 0 : value));
const group = fc.constantFrom("alpha", "beta", "gamma");

type Row = Map<string, JsonValue>;

const row: fc.Arbitrary<Row> = fc
  .record({
    int_value: fc.option(safeInt, { nil: null }),
    float_value: fc.option(fc.oneof(safeInt, safeFloat), { nil: null }),
    group,
    optional: fc.option(fc.option(safeInt, { nil: null }), { nil: undefined }),
  })
  .map(({ int_value, float_value, group: name, optional }) => {
    const entry: Row = new Map<string, JsonValue>([
      ["int_value", int_value],
      ["float_value", float_value],
      ["group", name],
    ]);
    if (optional !== undefined) {
      entry.set("optional", optional);
    }
    return entry;
  });

const rows: fc.Arbitrary<Row[]> = fc
  .tuple(fc.array(row, { minLength: 2, maxLength: 500 }), safeInt, safeFloat, safeInt, safeInt)
  .map(([generated, firstInt, firstFloat, secondFloat, mixed]) => {
    const [first, second, ...rest] = generated.map((entry) => new Map(entry));
    assert.ok(first !== undefined && second !== undefined);
    first.set("int_value", firstInt);
    first.set("float_value", firstFloat);
    first.set("mixed_value", mixed);
    first.delete("optional");
    second.set("float_value", secondFloat);
    second.set("mixed_value", "a string");
    second.set("optional", null);
    return [first, second, ...rest];
  });

function payload(items: readonly JsonObject[]): string {
  return dumpJson(new Map([["items", [...items]]]));
}

interface Materialized {
  readonly table: string;
  readonly columns: ReadonlyMap<string, { readonly type: string; readonly exact: boolean }>;
  readonly text: string;
}

async function withSession<T>(work: (interceptor: Interceptor, query: QueryTool) => Promise<T>): Promise<T> {
  const limits = makeLimits();
  const store = await Store.open(limits);
  try {
    return await work(new Interceptor(store, limits, { queryAvailable: true }), new QueryTool(store, limits));
  } finally {
    store.close();
  }
}

async function materialize(interceptor: Interceptor, items: readonly JsonObject[], tool = "generated_rows"): Promise<Materialized> {
  const result: ToolResult = { content: [{ type: "text", text: payload(items) }] };
  const handle = await interceptor.intercept({
    server: "generated",
    tool,
    mounted: "generated__rows",
    arguments: null,
    result,
    meta: null,
    startedAt: new Date(),
  });
  const structured = handle.structuredContent as {
    tables: { name: string; columns: { name: string; type: string; exact: boolean }[] }[];
  };
  const [table] = structured.tables;
  assert.ok(table !== undefined, "expected a table");
  return {
    table: table.name,
    columns: new Map(table.columns.map((column) => [column.name, column])),
    text: String(handle.content[0]?.text),
  };
}

function dataRows(markdown: string): string[][] {
  const lines = markdown.split("\n").filter((line) => line.startsWith("|"));
  assert.ok(lines.length >= 3, markdown);
  return lines.slice(2).map((line) => line.slice(1, -1).split("|").map((cell) => cell.trim()));
}

async function firstRow(query: QueryTool, sql: string): Promise<string[]> {
  const [first] = dataRows(await query.run(sql));
  assert.ok(first !== undefined);
  return first;
}

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) {
    return sorted[middle] ?? Number.NaN;
  }
  return ((sorted[middle - 1] ?? Number.NaN) + (sorted[middle] ?? Number.NaN)) / 2;
}

function integerMedian(values: readonly bigint[]): number {
  const sorted = [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) {
    return Number(sorted[middle] ?? 0n);
  }
  return Number((sorted[middle - 1] ?? 0n) + (sorted[middle] ?? 0n)) / 2;
}

function isClose(a: number, b: number, rel: number, abs: number): boolean {
  return a === b || Math.abs(a - b) <= Math.max(rel * Math.max(Math.abs(a), Math.abs(b)), abs);
}

function fmean(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function pythonSum(values: readonly number[]): number {
  let total = 0;
  let compensation = 0;
  for (const value of values) {
    const next = total + value;
    compensation += Math.abs(total) >= Math.abs(value) ? total - next + value : value - next + total;
    total = next;
  }
  return total + compensation;
}

async function assertGeneratedAggregates(items: readonly Row[], mixedExact: boolean | null = false): Promise<void> {
  await withSession(async (interceptor, query) => {
    const { table, columns } = await materialize(interceptor, items);
    assert.deepEqual(columns.get("int_value"), { name: "int_value", type: "BIGINT", exact: true, renamed_from: null });
    assert.equal(columns.get("float_value")?.type, "DOUBLE");
    assert.equal(columns.get("float_value")?.exact, false);
    assert.equal(columns.get("mixed_value")?.type, "VARCHAR");
    if (mixedExact !== null) {
      assert.equal(columns.get("mixed_value")?.exact, mixedExact);
    }
    const quoted = `"${table}"`;
    const ints = items.map((item) => item.get("int_value")).filter((value): value is bigint => typeof value === "bigint");
    const floats = items
      .map((item) => item.get("float_value"))
      .filter((value): value is bigint | number => typeof value === "bigint" || typeof value === "number")
      .map(Number);
    const optional = items.map((item) => item.get("optional") ?? null).filter((value): value is bigint => value !== null);

    const aggregate = await firstRow(
      query,
      `SELECT count(*), count(int_value), min(int_value), max(int_value), count(DISTINCT int_value), ` +
        `sum(int_value), median(int_value), count(optional), count(DISTINCT optional) FROM ${quoted}`,
    );
    assert.equal(Number(aggregate[0]), items.length);
    assert.equal(Number(aggregate[1]), ints.length);
    assert.equal(BigInt(aggregate[2] ?? ""), ints.reduce((a, b) => (b < a ? b : a)));
    assert.equal(BigInt(aggregate[3] ?? ""), ints.reduce((a, b) => (b > a ? b : a)));
    assert.equal(Number(aggregate[4]), new Set(ints).size);
    assert.equal(BigInt(aggregate[5] ?? ""), ints.reduce((a, b) => a + b, 0n));
    assert.equal(Number(aggregate[6]), integerMedian(ints));
    assert.equal(Number(aggregate[7]), optional.length);
    assert.equal(Number(aggregate[8]), new Set(optional).size);

    const floatAggregate = await firstRow(
      query,
      `SELECT count(float_value), min(float_value), max(float_value), count(DISTINCT float_value), median(float_value) FROM ${quoted}`,
    );
    assert.equal(Number(floatAggregate[0]), floats.length);
    assert.equal(Number(floatAggregate[1]), Math.min(...floats));
    assert.equal(Number(floatAggregate[2]), Math.max(...floats));
    assert.equal(Number(floatAggregate[3]), new Set(floats).size);
    assert.equal(Number(floatAggregate[4]), median(floats));

    const grouped = dataRows(await query.run(`SELECT "group", count(*) FROM ${quoted} GROUP BY "group" ORDER BY "group"`));
    const expected = new Map<string, number>();
    for (const item of items) {
      const name = String(item.get("group"));
      expected.set(name, (expected.get(name) ?? 0) + 1);
    }
    assert.deepEqual(
      grouped.map(([name, count]) => [name, Number(count)]),
      [...expected].sort(([a], [b]) => (a < b ? -1 : 1)),
    );
  });
}

test("property: aggregates match the normalized source", { timeout: 300_000 }, async () => {
  await fc.assert(
    fc.asyncProperty(rows, (items) => assertGeneratedAggregates(items)),
    { numRuns: 25, endOnFailure: true },
  );
});

test("property: float tolerances hold on bounded dyadic values", { timeout: 300_000 }, async () => {
  await fc.assert(
    fc.asyncProperty(fc.array(fc.integer({ min: -4000, max: 4000 }), { minLength: 1, maxLength: 500 }), async (coefficients) => {
      const values = coefficients.map((coefficient) => coefficient / 4);
      await withSession(async (interceptor, query) => {
        const items = values.map((value) => new Map<string, JsonValue>([["value", value]]));
        const { table, columns } = await materialize(interceptor, items, "bounded_float_regression");
        assert.equal(columns.get("value")?.type, "DOUBLE");
        assert.equal(columns.get("value")?.exact, false);
        const [average = "", sum = ""] = await firstRow(query, `SELECT avg(value), sum(value) FROM "${table}"`);
        assert.ok(isClose(Number(average), fmean(values), 1e-9, 1e-12), `avg ${average}`);
        assert.ok(isClose(Number(sum), pythonSum(values), 1e-9, 1e-12), `sum ${sum}`);
      });
    }),
    { numRuns: 25 },
  );
});

const FIXED: readonly (readonly [readonly JsonValue[], string, number, bigint | number])[] = [
  [[0n, 2n ** 63n - 2n, 2n ** 63n - 1n], "median(value)", 9.223372036854776e18, 2n ** 63n - 2n],
  [[1e308, 1e308], "median(value)", 1e308, Number.POSITIVE_INFINITY],
  [[-1e308, 1.0, 2.0, 1e308], "avg(value)", 0, 0.75],
  [[9007199254740993n, 0.5], "max(value)", 9007199254740992, 9007199254740993n],
];

for (const [values, expression, duckValue, reference] of FIXED) {
  test(`out-of-domain case ${expression} over ${values.map(String).join(", ")} is explicitly inexact`, async () => {
    await withSession(async (interceptor, query) => {
      const items = values.map((value) => new Map<string, JsonValue>([["value", value]]));
      const { table, columns, text } = await materialize(interceptor, items, "fixed_counterexample");
      assert.equal(columns.get("value")?.exact, false);
      assert.ok(text.includes("(inexact)"));
      const [observed = ""] = await firstRow(query, `SELECT ${expression} FROM "${table}"`);
      assert.equal(Number(observed), duckValue);
      const same = typeof reference === "bigint" ? BigInt(Number(observed)) === reference : reference === Number(observed);
      assert.ok(!same, `the reference ${String(reference)} matches ${observed}`);
    });
  });
}

test("float cancellation is a bounded regression, not a guarantee", async () => {
  const values = [1e6, 1e-10, -1e6];
  await withSession(async (interceptor, query) => {
    const items = values.map((value) => new Map<string, JsonValue>([["value", value]]));
    const { table, columns } = await materialize(interceptor, items, "float_cancellation");
    assert.equal(columns.get("value")?.type, "DOUBLE");
    assert.equal(columns.get("value")?.exact, false);
    const [average = "", sum = ""] = await firstRow(query, `SELECT avg(value), sum(value) FROM "${table}"`);
    assert.ok(!isClose(Number(average), 1e-10 / 3, 1e-9, 1e-12));
    assert.ok(!isClose(Number(sum), pythonSum(values), 1e-9, 1e-12));
  });
});

for (const count of [1, 500]) {
  test(`advertised row boundary ${count} is materialized`, async () => {
    const items = Array.from({ length: count }, (_, index) => {
      const mixed: JsonValue = count === 1 ? "only" : index % 2 === 0 ? BigInt(index) : "mixed";
      return new Map<string, JsonValue>([
        ["int_value", BigInt(index)],
        ["float_value", index + 0.5],
        ["group", index % 2 === 0 ? "alpha" : "beta"],
        ["mixed_value", mixed],
        ["optional", index === 0 ? null : BigInt(index)],
      ]);
    });
    await assertGeneratedAggregates(items, count === 1);
  });
}
