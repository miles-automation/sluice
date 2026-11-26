import assert from "node:assert/strict";
import { test } from "node:test";
import fc from "fast-check";
import {
  assertInjective,
  collectionName,
  MAX_MOUNTED_NAME,
  mountedName,
  NameCollisionError,
  NameTooLongError,
  quoteIdent,
  slug,
  tableName,
  tag,
} from "../src/naming.ts";

const name = fc.oneof(fc.string({ unit: "binary", maxLength: 60 }), fc.stringMatching(/^[A-Za-z0-9_.-]{1,80}$/u));

test("a forced collision fails loudly", () => {
  assert.throws(() => assertInjective([["s", "a"], ["s", "b"]], () => "same"), NameCollisionError);
  assert.equal(assertInjective([["s", "a"], ["s", "a"]], () => "same").size, 1);
});

test("a name over the limit fails loudly rather than truncating", () => {
  assert.throws(() => mountedName("server", "tool", 10), NameTooLongError);
  assert.throws(() => collectionName("server", "tool", 10), NameTooLongError);
});

test("a lone surrogate has no UTF-8 identity and is refused", () => {
  assert.throws(() => tag("a\ud800"), TypeError);
});

test("property: slugs are bounded identifiers", () => {
  fc.assert(
    fc.property(name, (value) => {
      const result = slug(value);
      assert.match(result, /^[a-z0-9][a-z0-9_]{0,39}$/u);
    }),
  );
});

test("property: names are built from slugs and a 12-hex tag of the exact identity", () => {
  fc.assert(
    fc.property(name, name, (server, tool) => {
      const mounted = mountedName(server, tool);
      assert.ok(mounted.length <= MAX_MOUNTED_NAME);
      assert.match(tag(`${server}\u0000${tool}`), /^[0-9a-f]{12}$/u);
      assert.equal(mounted, `${slug(server)}__${slug(tool)}__${tag(`${server}\u0000${tool}`)}`);
      assert.notEqual(collectionName(server, tool), mounted);
    }),
  );
});

test("property: distinct pairs mount to distinct names", () => {
  fc.assert(
    fc.property(fc.uniqueArray(fc.tuple(name, name), { selector: ([s, t]) => `${s}\u0000${t}`, maxLength: 20 }), (pairs) => {
      assert.equal(assertInjective(pairs).size, pairs.length);
    }),
  );
});

test("property: quoting round-trips any identifier", () => {
  fc.assert(
    fc.property(fc.string({ unit: "binary" }), (value) => {
      const quoted = quoteIdent(value);
      const inner = quoted.slice(1, -1);
      assert.ok(quoted.startsWith('"') && quoted.endsWith('"'));
      assert.doesNotMatch(inner.replaceAll('""', ""), /"/u);
      assert.equal(inner.replaceAll('""', '"'), value);
    }),
  );
});

test("property: the table sequence is zero-padded to 4 and widens", () => {
  fc.assert(
    fc.property(fc.integer({ min: 0, max: 10_000_000 }), (seq) => {
      const suffix = tableName("m", "s", seq).slice("m__s__".length);
      assert.equal(Number(suffix), seq);
      assert.equal(suffix.length, Math.max(4, String(seq).length));
    }),
  );
});
