import assert from "node:assert/strict";
import { mock, test } from "node:test";
import fc from "fast-check";
import { derive, fromConversationId, mint, SCOPE_META_KEYS } from "../src/scope.ts";

const HEX32 = /^[0-9a-f]{32}$/u;

test("minting draws from the platform CSPRNG", () => {
  const spy = mock.method(globalThis.crypto, "getRandomValues");
  try {
    assert.match(mint(), HEX32);
    assert.equal(spy.mock.callCount(), 1);
    const [buffer] = spy.mock.calls[0]?.arguments ?? [];
    assert.ok(buffer instanceof Uint8Array && buffer.length === 16);
  } finally {
    spy.mock.restore();
  }
});

test("derive reads a Map as well as a plain object", () => {
  assert.deepEqual(derive(new Map([["sessionId", "same"]])), { tag: fromConversationId("same"), fromClient: true });
  assert.equal(derive(["conversationId"]).fromClient, false);
  assert.equal(derive(Object.create({ conversationId: "inherited" })).fromClient, false);
});

test("property: a conversation id gives a stable 128-bit scope", () => {
  fc.assert(
    fc.property(fc.string({ unit: "binary" }), fc.string({ unit: "binary" }), (a, b) => {
      assert.match(fromConversationId(a), HEX32);
      assert.equal(fromConversationId(a), fromConversationId(a));
      if (a !== b) {
        assert.notEqual(fromConversationId(a), fromConversationId(b));
      }
    }),
  );
});

test("property: derive takes the first non-empty string key in order", () => {
  const entry = fc.tuple(fc.constantFrom(...SCOPE_META_KEYS, "other"), fc.oneof(fc.string({ maxLength: 4 }), fc.integer()));
  fc.assert(
    fc.property(fc.uniqueArray(entry, { selector: ([name]) => name }), (entries) => {
      const meta = Object.fromEntries(entries);
      const winner = SCOPE_META_KEYS.map((name) => meta[name]).find((value) => typeof value === "string" && value !== "");
      const derived = derive(meta);
      assert.equal(derived.fromClient, winner !== undefined);
      assert.match(derived.tag, HEX32);
      if (typeof winner === "string") {
        assert.equal(derived.tag, fromConversationId(winner));
      }
    }),
  );
});
