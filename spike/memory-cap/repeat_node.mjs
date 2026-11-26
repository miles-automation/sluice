import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const base = process.env.DUCKDB_NODE_BASE ?? join(here, "../../typescript/package.json");
const { DuckDBInstance } = createRequire(base)("@duckdb/node-api");
const probe = process.argv[2];
const sql = JSON.parse(readFileSync(join(here, "probes.json"), "utf8"))[probe];
const instance = await DuckDBInstance.create(":memory:");
const setup = await instance.connect();
await setup.run("CREATE TABLE t AS SELECT i::INTEGER AS i, 'a' AS s FROM range(100) r(i)");
await setup.run("SET temp_directory = ''");
await setup.run("SET max_memory = '256MB'");
await setup.run("SET lock_configuration = true");
const after = [];
for (let n = 0; n < 4; n += 1) {
  const c = await instance.connect();
  await c.runAndReadAll(sql);
  c.closeSync();
  after.push(Math.round(process.memoryUsage.rss() / 2 ** 20));
}
console.log(JSON.stringify({ lang: "node", probe, rss_after_each_mb: after }));
process.exit(0);
