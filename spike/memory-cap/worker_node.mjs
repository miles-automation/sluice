import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const here = dirname(fileURLToPath(import.meta.url));
const base = process.env.DUCKDB_NODE_BASE ?? join(here, "../../typescript/package.json");
const { DuckDBInstance, version } = createRequire(base)("@duckdb/node-api");

const MB = 2 ** 20;
const { values } = parseArgs({
  options: {
    probe: { type: "string" },
    probes: { type: "string", default: join(here, "probes.json") },
    "max-memory": { type: "string", default: "256MB" },
    "watchdog-mb": { type: "string", default: "0" },
    "interval-ms": { type: "string", default: "20" },
    set: { type: "string", multiple: true, default: [] },
  },
});
const sql = JSON.parse(readFileSync(values.probes, "utf8"))[values.probe];
const watchdogBytes = Number(values["watchdog-mb"]) * MB;
const mb = (v) => Math.round(v / MB);
const rss = () => process.memoryUsage.rss();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const report = { lang: "node", duckdb: version(), probe: values.probe };
const instance = await DuckDBInstance.create(":memory:");
const setup = await instance.connect();
await setup.run("CREATE TABLE t AS SELECT i::INTEGER AS i, 'a' AS s FROM range(100) r(i)");
for (const s of [
  "SET temp_directory = ''",
  "SET enable_external_access = false",
  "SET autoinstall_known_extensions = false",
  "SET autoload_known_extensions = false",
  "SET allow_community_extensions = false",
  `SET max_memory = '${values["max-memory"]}'`,
  ...values.set,
  "SET lock_configuration = true",
]) {
  await setup.run(s);
}
setup.closeSync();
report.rss_setup_mb = mb(rss());

const connection = await instance.connect();
let peak = 0;
let samples = 0;
let firedAt = null;
let firedRss = null;
const started = performance.now();
const timer = setInterval(() => {
  const value = rss();
  samples += 1;
  peak = Math.max(peak, value);
  if (watchdogBytes && firedAt === null && value > watchdogBytes) {
    firedAt = performance.now();
    firedRss = value;
    connection.interrupt();
  }
}, Number(values["interval-ms"]));
try {
  const reader = await connection.runAndReadAll(sql);
  report.outcome = "completed";
  report.result = JSON.stringify(reader.getRowsJS(), (_, v) => (typeof v === "bigint" ? v.toString() : v)).slice(0, 80);
} catch (error) {
  const message = String(error?.message ?? error);
  report.outcome = /out of memory/i.test(message)
    ? "duckdb_oom"
    : /interrupt/i.test(message)
      ? "interrupted"
      : "duckdb_error";
  report.error = message.slice(0, 200);
}
const ended = performance.now();
clearInterval(timer);
connection.closeSync();
report.query_s = Math.round((ended - started) / 10) / 100;
report.watchdog_samples = samples;
report.watchdog_peak_mb = mb(peak);
if (firedAt !== null) {
  report.interrupt_rss_mb = mb(firedRss);
  report.interrupt_after_s = Math.round((firedAt - started) / 10) / 100;
  report.interrupt_to_return_s = Math.round((ended - firedAt) / 10) / 100;
}
report.rss_after_mb = mb(rss());
try {
  const check = await instance.connect();
  const rows = (await check.runAndReadAll("SELECT count(*)::INTEGER, sum(i)::INTEGER FROM t")).getRowsJS();
  await check.run("CREATE TABLE z AS SELECT i, s || i AS s FROM t");
  await check.run("DROP TABLE z");
  check.closeSync();
  report.usable = JSON.stringify(rows) === "[[100,4950]]";
} catch (error) {
  report.usable = false;
  report.usable_error = String(error?.message ?? error).slice(0, 160);
}
await sleep(1000);
report.rss_after_1s_mb = mb(rss());
report.release = "none available from JS";
report.rss_after_release_mb = mb(rss());
console.log(JSON.stringify(report));
process.exit(0);
