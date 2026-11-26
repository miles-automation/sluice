import { makeLimits } from "../../src/limits.ts";
import { QueryTool } from "../../src/query.ts";
import { Store } from "../../src/store.ts";

const UNBOUNDED = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r";
const count = Number(process.argv[2] ?? "12");
const limits = makeLimits({ query_timeout_seconds: 0.5 });
const store = await Store.open(limits);
const query = new QueryTool(store, limits, { concurrency: count });
const started = Date.now();
const outcomes = await Promise.allSettled(Array.from({ length: count }, () => query.run(UNBOUNDED)));
const responded = Date.now() - started;
while (query.inFlight > 0) {
  await new Promise((resolve) => setTimeout(resolve, 20));
}
const released = Date.now() - started;
const answer = await query.run("SELECT 42 AS answer");
store.close();
process.stdout.write(
  JSON.stringify({
    timeouts: outcomes.filter((outcome) => outcome.status === "rejected" && /timeout/u.test(String(outcome.reason))).length,
    responded,
    released,
    answered: answer.includes("| 42 |"),
  }),
);
