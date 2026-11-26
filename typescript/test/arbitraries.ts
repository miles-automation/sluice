import fc from "fast-check";
import type { JsonObject, JsonValue } from "../src/json.ts";

export const text = fc.string({ unit: "binary", maxLength: 12 });

export const key = fc.oneof(
  fc.constantFrom("_row", "_call_id", "_extra", "_row__src", "_row__src2", "value", "1", "0", "__proto__", "a", "b"),
  text,
);

export const integer = fc.oneof(
  fc.bigInt({ min: -(2n ** 54n), max: 2n ** 54n }),
  fc.bigInt({ min: -(2n ** 130n), max: 2n ** 130n }),
  fc.constantFrom(2n ** 53n, -(2n ** 53n), 2n ** 53n + 1n, 2n ** 63n - 1n, -(2n ** 63n), 2n ** 63n, 2n ** 127n - 1n, 2n ** 127n),
);

export const float = fc.oneof(fc.double(), fc.constantFrom(0, -0, 1, 0.5, 1e16, 1e-5, Number.MAX_VALUE, Number.MIN_VALUE));

export const scalar: fc.Arbitrary<JsonValue> = fc.oneof(fc.constant(null), fc.boolean(), integer, float, text);

export function objectOf(value: fc.Arbitrary<JsonValue>, maxKeys = 6): fc.Arbitrary<JsonObject> {
  return fc
    .uniqueArray(fc.tuple(key, value), { selector: (entry) => entry[0], maxLength: maxKeys })
    .map((entries) => new Map(entries));
}

export const json: fc.Arbitrary<JsonValue> = fc.letrec<{ value: JsonValue }>((tie) => ({
  value: fc.oneof(
    { depthSize: "small", withCrossShrink: true },
    scalar,
    fc.array(tie("value"), { maxLength: 4 }),
    objectOf(tie("value"), 4),
  ),
})).value;

export const row = objectOf(json, 8);
