import { configureThreadpool, threadpoolSize } from "../../src/threadpool.ts";

const requested = Number(process.argv[2] ?? "2");
configureThreadpool(requested);
const { DuckDBInstance } = await import("@duckdb/node-api");

const SLOW = "SELECT count(*) FROM generate_series(1, 100000000000) a, generate_series(1, 100) b";
const instance = await DuckDBInstance.create(":memory:");
const blockers = await Promise.all(Array.from({ length: requested }, () => instance.connect()));
const late = await instance.connect();
const running = blockers.map((connection) => connection.runAndReadAll(SLOW).catch((error: unknown) => error));
await new Promise((resolve) => setTimeout(resolve, 200));
let lateDone = false;
const lateQuery = late.runAndReadAll("SELECT 1").then(() => {
  lateDone = true;
});
await new Promise((resolve) => setTimeout(resolve, 500));
const queued = !lateDone;
for (const connection of blockers) {
  connection.interrupt();
}
await Promise.all(running);
await lateQuery;
for (const connection of [...blockers, late]) {
  connection.closeSync();
}
instance.closeSync();
process.stdout.write(JSON.stringify({ size: threadpoolSize(), queued }));
