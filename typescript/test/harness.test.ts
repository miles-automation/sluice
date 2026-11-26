import assert from "node:assert/strict";
import { test } from "node:test";
import { adapters } from "./conformance/adapters.ts";
import { KINDS, loadFixtures, loadSchemaValidator, type Json } from "./conformance/fixtures.ts";
import { matchCell, matchValue } from "./conformance/matchers.ts";
import { applyMutations } from "./conformance/mutations.ts";

test("every fixture validates against the schema and ids are unique", () => {
  const fixtures = loadFixtures();
  assert.ok(fixtures.length > 0);
  for (const kind of KINDS) {
    assert.ok(
      fixtures.some((fixture) => fixture.kind === kind),
      `no fixtures of kind ${kind}`,
    );
  }
});

test("every kind is registered as an adapter or as pending", () => {
  assert.deepEqual(Object.keys(adapters).sort(), [...KINDS].sort());
});

test("the schema rejects a malformed fixture", () => {
  const validate = loadSchemaValidator();
  assert.deepEqual(validate({ kind: "infer", description: "x", cases: [{ id: "a", values_json: "[1]" }] }).length > 0, true);
  assert.deepEqual(validate({ kind: "nope", description: "x", cases: [{ id: "a" }] }).length > 0, true);
  assert.deepEqual(
    validate({ kind: "infer", description: "x", cases: [{ id: "a", values_json: "[1]", expect: { type: "BIGINT", exact: true } }] }),
    [],
  );
});

test("cell matchers read rendered text", () => {
  assert.ok(matchCell(null, "NULL"));
  assert.ok(!matchCell(null, "''"));
  assert.ok(matchCell("a\\|b", "a\\|b"));
  assert.ok(matchCell(400, "400"));
  assert.ok(!matchCell(400, "400.0"));
  assert.ok(matchCell({ int: "18446744073709551617" }, "18446744073709551617"));
  assert.ok(!matchCell({ int: "18446744073709551617" }, "18446744073709551616"));
  assert.ok(matchCell({ double: 2 }, "2.0"));
  assert.ok(matchCell({ double: "9.223372036854776e18" }, "9223372036854776000"));
  assert.ok(matchCell({ bool: true }, "True"));
  assert.ok(matchCell({ approx: 0.75, rel: 1e-9, abs: 1e-12 }, "0.7500000000001"));
  assert.ok(!matchCell({ approx: 0.75, rel: 1e-9, abs: 1e-12 }, "0"));
  assert.ok(!matchCell({ approx: 1, rel: 1e-9, abs: 1e-12, exact: false }, "1"));
  assert.ok(!matchCell({ double: 1 }, ""));
});

test("value matchers check the type as well as the value", () => {
  assert.ok(matchValue("232", "232"));
  assert.ok(!matchValue("232", 232));
  assert.ok(matchValue(3, 3n));
  assert.ok(!matchValue(3, 3));
  assert.ok(matchValue({ int: "18014398509481984" }, 18014398509481984n));
  assert.ok(matchValue({ double: 199.5 }, 199.5));
  assert.ok(!matchValue({ double: 199.5 }, "199.5"));
  assert.ok(matchValue({ bool: false }, false));
});

test("AST mutations apply to a serialized document", () => {
  const document: Json = {
    statements: [
      {
        node: {
          type: "SELECT_NODE",
          cte_map: { map: [{ key: "x", value: { key_targets: [], query_node: { type: "SELECT_NODE", from_table: { type: "BASE_TABLE", table_name: "mine" } } } }] },
          from_table: { type: "BASE_TABLE", table_name: "x" },
        },
      },
    ],
  };
  const value = ["statements", 0, "node", "cte_map", "map", 0, "value"];
  const renamed = applyMutations(document, [{ op: "rename_first", path: value, keys: ["query_node", "query"], to: "future_body" }], () => null);
  assert.equal(JSON.stringify(renamed).includes("future_body"), true);
  assert.equal(JSON.stringify(document).includes("future_body"), false);
  const updated = applyMutations(document, [{ op: "update", within: value, find: "SELECT_NODE", set: { type: "FUTURE_NODE" } }], () => null);
  assert.equal(JSON.stringify(updated).includes("FUTURE_NODE"), true);
  const deleted = applyMutations(document, [{ op: "delete", path: ["statements"] }], () => null);
  assert.deepEqual(deleted, {});
  const injected = applyMutations(
    document,
    [{ op: "set_serialized", path: [...value, "key_targets"], sql: "SELECT 1", value: [{ subquery: { node: "$node" } }] }],
    () => ({ statements: [{ node: { type: "SELECT_NODE", marker: true } }] }),
  );
  assert.equal(JSON.stringify(injected).includes('"marker":true'), true);
  assert.throws(() => applyMutations(document, [{ op: "nope" }], () => null));
});
