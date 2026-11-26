import { describe, test } from "node:test";
import { adapters } from "./conformance/adapters.ts";
import { KINDS, loadFixtures } from "./conformance/fixtures.ts";

const fixtures = loadFixtures();

for (const kind of KINDS) {
  const entry = adapters[kind];
  const cases = fixtures.filter((fixture) => fixture.kind === kind).flatMap((fixture) => fixture.cases);
  describe(`conformance: ${kind}`, () => {
    if ("pending" in entry) {
      const reason = `kind '${kind}' has no TypeScript implementation yet: ${entry.pending}`;
      test(`${cases.length} case(s) skipped`, { skip: reason }, () => {});
      for (const testCase of cases) {
        test(testCase.id, { skip: reason }, () => {});
      }
      return;
    }
    for (const testCase of cases) {
      test(testCase.id, () => entry(testCase));
    }
  });
}
