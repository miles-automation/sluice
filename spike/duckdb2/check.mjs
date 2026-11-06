// Task 1074: does @duckdb/node-api 2.x have the primitives Sluice depends on?
// Each check proves the behaviour. Prints a PASS or FAIL table, writes
// results-<platform>-<arch>.json, and exits 1 on any failure.
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { arch, platform, tmpdir } from "node:os";
import { DuckDBInstance, StatementType, version as bindingVersion } from "@duckdb/node-api";

const here = dirname(fileURLToPath(import.meta.url));
setTimeout(() => {
  console.error("WATCHDOG: a check hung for 120 s");
  process.exit(2);
}, 120_000).unref();
const results = [];

async function refused(work) {
  try {
    await work();
    return null;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}

async function check(name, work) {
  const started = Date.now();
  try {
    const detail = await work();
    results.push({ name, ok: true, detail: String(detail ?? ""), ms: Date.now() - started });
  } catch (error) {
    results.push({
      name,
      ok: false,
      detail: (error instanceof Error ? error.message : String(error)).slice(0, 400),
      ms: Date.now() - started,
    });
  }
}

function must(condition, message) {
  if (!condition) throw new Error(message);
}

const rows = async (connection, sql) =>
  (await connection.runAndReadAll(sql)).getRowObjectsJS();

const scalar = async (connection, sql) => {
  const reader = await connection.runAndReadAll(sql);
  return reader.getRowsJS()[0][0];
};

// Same order as store.py, lock last.
async function lockedDown(maxMemory = "256MB") {
  const instance = await DuckDBInstance.create(":memory:");
  const connection = await instance.connect();
  await connection.run("CREATE TABLE t(a INTEGER, b VARCHAR, d DOUBLE)");
  await connection.run(
    "INSERT INTO t SELECT i, 'row' || i, i / 2.0 FROM range(1, 11) AS r(i)",
  );
  for (const statement of [
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET allow_community_extensions = false",
    `SET max_memory = '${maxMemory}'`,
    "SET lock_configuration = true",
  ])
    await connection.run(statement);
  return { instance, connection };
}

const engine = await (async () => {
  const instance = await DuckDBInstance.create(":memory:");
  const connection = await instance.connect();
  const value = await scalar(connection, "SELECT version()");
  connection.closeSync();
  instance.closeSync();
  return value;
})();

const { instance, connection } = await lockedDown();
const secret = join(tmpdir(), `sluice-1074-${process.pid}.txt`);
await writeFile(secret, "secret\n");
const jsonFile = join(tmpdir(), `sluice-1074-${process.pid}.json`);
await writeFile(jsonFile, '[{"a":1}]\n');

await check("statement typing: extractStatements counts and types", async () => {
  const one = await connection.extractStatements("SELECT 1");
  must(one.count === 1, "one statement");
  must((await one.prepare(0)).statementType === StatementType.SELECT, "SELECT typed");
  const two = await connection.extractStatements("SELECT 1; SELECT 2");
  must(two.count === 2, "two statements counted");
  // Python returns an empty list here. The Node binding throws an opaque
  // "Error in native callback", which still fails closed.
  for (const nothing of ["-- nothing\n", "", " ;"])
    must(await refused(() => connection.extractStatements(nothing)), `${JSON.stringify(nothing)} is refused`);
  const insert = await connection.extractStatements("INSERT INTO t VALUES (1, 'x', 1)");
  must((await insert.prepare(0)).statementType === StatementType.INSERT, "INSERT typed");
  const rowsBefore = await scalar(connection, "SELECT count(*) FROM t");
  must(Number(rowsBefore) === 10, "preparing did not execute the INSERT");
  const drop = await connection.extractStatements("DROP TABLE t");
  must((await drop.prepare(0)).statementType === StatementType.DROP, "DROP typed");
  must(Number(await scalar(connection, "SELECT count(*) FROM t")) === 10, "t still exists");
  const syntax = await refused(() => connection.extractStatements("SELEC 1"));
  must(syntax, "a syntax error is refused");
  return "count without binding; type needs prepare (which binds)";
});

await check("statement typing: type comes only from prepare, which binds", async () => {
  // Python's extract_statements types without binding. Here an unknown table
  // fails at prepare, so a type-first gate would leak whether a table exists.
  const missing = await connection.extractStatements("SELECT * FROM no_such_table");
  must(missing.count === 1, "extract does not bind");
  const message = await refused(() => missing.prepare(0));
  must(message && /no_such_table|does not exist/i.test(message), "prepare binds");
  return "run the AST allowlist before prepare, or the error is an existence oracle";
});

await check("statement typing: SHOW, DESCRIBE, PRAGMA pass the type check as in 1.5.5", async () => {
  const seen = {};
  for (const sql of ["SHOW TABLES", "DESCRIBE t", "SUMMARIZE t", "PRAGMA show_tables"]) {
    const extracted = await connection.extractStatements(sql);
    must(extracted.count === 1, `${sql} one statement`);
    const message = await refused(async () => {
      seen[sql] = StatementType[(await extracted.prepare(0)).statementType];
    });
    if (message) seen[sql] = `refused: ${message.slice(0, 60)}`;
  }
  return JSON.stringify(seen);
});

await check("json_serialize_sql: table refs, CTE map, table functions, errors", async () => {
  const ast = async (sql) =>
    JSON.parse(
      await scalar(connection, `SELECT json_serialize_sql(${quote(sql)})`),
    );
  const walk = (node, seen = []) => {
    if (Array.isArray(node)) node.forEach((item) => walk(item, seen));
    else if (node && typeof node === "object") {
      if (typeof node.type === "string") seen.push(node);
      Object.values(node).forEach((item) => walk(item, seen));
    }
    return seen;
  };
  const base = walk(await ast("SELECT a FROM t")).find((n) => n.type === "BASE_TABLE");
  must(base?.table_name === "t", "BASE_TABLE.table_name");
  const qualified = walk(await ast("SELECT * FROM main.t")).find((n) => n.type === "BASE_TABLE");
  must(qualified?.schema_name === "main", "BASE_TABLE.schema_name");
  const functionRef = walk(await ast("SELECT * FROM range(5)")).find((n) => n.type === "TABLE_FUNCTION");
  must(functionRef?.function?.function_name === "range", "TABLE_FUNCTION.function.function_name");
  const scalarCall = walk(await ast("SELECT lpad('x', 3, 'y')")).find((n) => n.type === "FUNCTION");
  must(scalarCall?.function_name === "lpad", "FUNCTION.function_name");
  const cte = await ast("WITH c AS (SELECT 1) SELECT * FROM c");
  const node = cte.statements[0].node;
  must(node.cte_map?.map?.[0]?.key === "c", "cte_map.map[].key");
  // 1.5.5 was value.query.node. gate.py reads that path, so its logic does not
  // carry over unchanged: on 2.0 it skips every CTE body.
  must(node.cte_map.map[0].value?.query === undefined, "2.0 has no value.query");
  must(node.cte_map.map[0].value?.query_node?.type === "SELECT_NODE", "2.0 value.query_node");
  // A generic walk still reaches a table inside a CTE body and function args.
  const inside = walk(await ast("WITH x AS (SELECT * FROM hidden) SELECT * FROM x")).filter((n) => n.type === "BASE_TABLE").map((n) => n.table_name);
  must(inside.includes("hidden"), "walk reaches the CTE body");
  const nested = walk(await ast("SELECT upper(lpad('x', 3, 'y'))")).filter((n) => n.type === "FUNCTION").map((n) => n.function_name);
  must(nested.includes("lpad"), "walk reaches function arguments (arguments[].expression in 2.0)");
  const recursive = await ast(
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT * FROM r",
  );
  const recursiveNode = recursive.statements[0].node.cte_map.map[0].value.query_node;
  must(recursiveNode.type === "RECURSIVE_CTE_NODE", "RECURSIVE_CTE_NODE");
  must(recursiveNode.left && recursiveNode.right, "recursive left and right");
  const show = walk(await ast("SHOW TABLES")).find((n) => n.type === "SHOW_REF");
  must(show, "SHOW_REF");
  const failed = await ast("INSERT INTO t VALUES (1)");
  must(failed.error === true, "non-SELECT serializes to error:true");
  const pragma = await refused(async () => {
    const document = await ast("PRAGMA show_tables");
    if (document.error) throw new Error("error document");
  });
  must(pragma, "PRAGMA does not serialize");
  return "field names the allowlist reads are all present";
});

await check("enable_external_access=false: file readers refused", async () => {
  const seen = [];
  for (const sql of [
    `SELECT * FROM read_text('${secret}')`,
    `SELECT * FROM read_json('${jsonFile}')`,
    `SELECT * FROM read_csv('${secret}')`,
    `SELECT * FROM '${secret}'`,
    `COPY t TO '${secret}.out'`,
    `ATTACH '${secret}.db' AS other`,
    "INSTALL httpfs",
    "LOAD httpfs",
  ]) {
    const message = await refused(() => connection.runAndReadAll(sql));
    must(message, `${sql} was not refused`);
    seen.push(message.slice(0, 50));
  }
  return seen[0];
});

await check("lock_configuration=true: settings cannot be changed back", async () => {
  for (const sql of [
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "SET max_memory = '64GB'",
    "RESET max_memory",
    "SET autoload_known_extensions = true",
  ]) {
    const message = await refused(() => connection.run(sql));
    must(message, `${sql} was not refused`);
  }
  const value = await scalar(connection, "SELECT current_setting('enable_external_access')");
  must(value === false, "still off");
  // A second connection on the instance sees the same lock: the lockdown is
  // database-global, and each query gets its own connection.
  const other = await instance.connect();
  must(await refused(() => other.run("SET enable_external_access = true")), "other connection refused");
  must(await refused(() => other.runAndReadAll(`SELECT * FROM read_text('${secret}')`)), "other connection cannot read files");
  other.closeSync();
  return "locked on this and new connections";
});

await check("max_memory: an oversized allocation fails loudly, store stays usable", async () => {
  const { instance: small, connection: smallConnection } = await lockedDown("64MB");
  const message = await refused(() =>
    smallConnection.runAndReadAll(
      "SELECT string_agg(repeat('x', 1024), '') AS s FROM (SELECT unnest(generate_series(1, 2000000)))",
    ),
  );
  must(message, "the allocation was not refused");
  must(/out of memory|memory/i.test(message), `not an OOM error: ${message.slice(0, 120)}`);
  must(Number(await scalar(smallConnection, "SELECT count(*) FROM t")) === 10, "store usable after OOM");
  const limit = await scalar(smallConnection, "SELECT current_setting('max_memory')");
  smallConnection.closeSync();
  small.closeSync();
  return `limit ${limit}; ${message.slice(0, 70)}`;
});

await check("interrupt: ends a running query, connection stays usable, event loop stays free", async () => {
  const worker = await instance.connect();
  let ticks = 0;
  const ticker = setInterval(() => ticks++, 10);
  const started = Date.now();
  const running = worker.runAndReadAll(
    "SELECT count(*) FROM generate_series(1, 100000000000) a, generate_series(1, 100) b",
  );
  setTimeout(() => worker.interrupt(), 200);
  const message = await refused(() => running);
  clearInterval(ticker);
  const elapsed = Date.now() - started;
  must(message, "the query finished instead of being interrupted");
  must(/interrupt/i.test(message), `not an interrupt error: ${message.slice(0, 120)}`);
  must(elapsed < 10000, `interrupt took ${elapsed} ms`);
  must(ticks > 5, `event loop blocked: ${ticks} ticks`);
  must(Number(await scalar(worker, "SELECT 41 + 1")) === 42, "same connection usable after interrupt");
  worker.closeSync();
  return `${elapsed} ms, ${ticks} loop ticks, ${message.slice(0, 40)}`;
});

await check("interrupt: applies only to its own connection", async () => {
  const victim = await instance.connect();
  const bystander = await instance.connect();
  const slow = victim.runAndReadAll(
    "SELECT count(*) FROM generate_series(1, 100000000000) a, generate_series(1, 100) b",
  );
  const other = bystander.runAndReadAll(
    "SELECT count(*) AS n FROM generate_series(1, 30000000)",
  );
  setTimeout(() => victim.interrupt(), 100);
  must(await refused(() => slow), "victim was interrupted");
  const answer = (await other).getRowsJS()[0][0];
  must(Number(answer) === 30000000, "bystander finished with the right answer");
  victim.closeSync();
  bystander.closeSync();
  return "bystander unaffected";
});

const SLOW = "SELECT count(*) FROM generate_series(1, 100000000000) a, generate_series(1, 100) b";

await check("one connection per in-flight query: a query finishes while another runs", async () => {
  const slowConnection = await instance.connect();
  const fast = await instance.connect();
  let slowDone = false;
  const slow = slowConnection.runAndReadAll(SLOW).catch((error) => {
    slowDone = true;
    return error;
  });
  await new Promise((resolve) => setTimeout(resolve, 100));
  must(!slowDone, "slow query is running");
  must(Number(await scalar(fast, "SELECT count(*) FROM t")) === 10, "fast query answered");
  must(!slowDone, "slow query was still running when the fast one answered");
  slowConnection.interrupt();
  must(/interrupt/i.test(String(await slow)), "slow query interrupted");
  // Transaction isolation between a writer and readers, as the envelope needs.
  await fast.run("BEGIN");
  await fast.run("CREATE TABLE tx(a INTEGER)");
  must(await refused(() => slowConnection.runAndReadAll("SELECT * FROM tx")), "uncommitted table is invisible");
  await fast.run("ROLLBACK");
  must(await refused(() => fast.runAndReadAll("SELECT * FROM tx")), "rolled back");
  slowConnection.closeSync();
  fast.closeSync();
  return "queries overlap on separate connections; transactional visibility holds";
});

await check("libuv pool: saturation delays new queries, interrupt still gets through", async () => {
  const size = Number(process.env.UV_THREADPOOL_SIZE ?? 4);
  const blockers = await Promise.all(Array.from({ length: size }, () => instance.connect()));
  const late = await instance.connect();
  const running = blockers.map((c) => c.runAndReadAll(SLOW).catch((error) => error));
  await new Promise((resolve) => setTimeout(resolve, 100));
  let lateDone = false;
  const lateQuery = late.runAndReadAll("SELECT 1").then(() => (lateDone = true));
  // connect() runs on the same pool, so it queues as well. Never put an awaited
  // DuckDB call between a query and the deadline that would interrupt it.
  let connectDone = false;
  const lateConnect = instance.connect().then((c) => {
    connectDone = true;
    return c;
  });
  await new Promise((resolve) => setTimeout(resolve, 500));
  const queued = !lateDone;
  const connectQueued = !connectDone;
  // interrupt is synchronous and off the pool, so a deadline still works.
  blockers.forEach((c) => c.interrupt());
  const stopped = await Promise.all(running);
  stopped.forEach((outcome) => must(/interrupt/i.test(String(outcome)), "blocker interrupted"));
  await lateQuery;
  must(lateDone, "the queued query ran after the pool freed up");
  (await lateConnect).closeSync();
  blockers.forEach((c) => c.closeSync());
  late.closeSync();
  return `pool ${size}: new query ${queued ? "QUEUED" : "not delayed"}, connect ${connectQueued ? "QUEUED" : "not delayed"} behind slow queries; deadlines still interrupt`;
});

await check("value fidelity: exact text is available past JS number precision", async () => {
  const reader = await connection.runAndReadAll(
    "SELECT 9007199254740993::BIGINT AS b, 170141183460469231731687303715884105727::HUGEINT AS h, 12345678901234567.12::DECIMAL(38,2) AS d, 0.1::DOUBLE AS f, NULL::INTEGER AS n, ''::VARCHAR AS e",
  );
  const row = reader.getRows()[0];
  must(String(row[0]) === "9007199254740993", `BIGINT text ${row[0]}`);
  must(String(row[1]) === "170141183460469231731687303715884105727", `HUGEINT text ${row[1]}`);
  must(String(row[2]) === "12345678901234567.12", `DECIMAL text ${row[2]}`);
  must(String(row[3]) === "0.1", `DOUBLE text ${row[3]}`);
  must(row[4] === null, "NULL is null");
  must(row[5] === "", "empty string stays distinct from NULL");
  const js = reader.getRowsJS()[0];
  const lossy = String(js[2]) !== "12345678901234567.12";
  return `render from getRows(), not getRowsJS(): getRowsJS DECIMAL ${lossy ? "LOSES precision" : "keeps precision"} (${js[2]})`;
});

await check("median and friends over typed columns", async () => {
  const typed = await instance.connect();
  await typed.run(`CREATE TABLE typed(i BIGINT, d DOUBLE, dec DECIMAL(10,2), big HUGEINT, s VARCHAR, j JSON)`);
  await typed.run(`INSERT INTO typed VALUES
    (1, 0.5, 1.10, 9007199254740993, '10', '10'),
    (2, 1.5, 2.20, 2, '9', '9'),
    (4, 2.5, 3.30, 3, '100', '100'),
    (100, 100.5, 4.40, 4, '2', '2')`);
  const row = (
    await rows(
      typed,
      `SELECT median(i) AS mi, median(d) AS md, median(dec) AS mdec, sum(i) AS si, avg(i) AS ai,
              min(big) AS mb, median(s) AS ms, median(j) AS mj FROM typed`,
    )
  )[0];
  must(Number(row.mi) === 3, `median BIGINT = 3, got ${row.mi}`);
  must(Number(row.md) === 2, `median DOUBLE = 2, got ${row.md}`);
  must(Number(row.mdec) === 2.75, `median DECIMAL = 2.75, got ${row.mdec}`);
  must(Number(row.si) === 107, `sum = 107, got ${row.si}`);
  must(Number(row.ai) === 26.75, `avg = 26.75, got ${row.ai}`);
  // spec 5.5: median on text and JSON is lexicographic. This is why mixed
  // scalars become VARCHAR and arithmetic on JSON needs a cast. Record it.
  const big = await scalar(typed, "SELECT max(big) FROM typed");
  must(String(big) === "9007199254740993", `HUGEINT past 2^53 keeps precision: ${big}`);
  must(typeof (await scalar(typed, "SELECT max(i) FROM typed")) === "bigint", "BIGINT arrives as bigint");
  typed.closeSync();
  return `median text ${row.ms}, median json ${row.mj} (lexicographic, as in 1.5.5)`;
});

await check("adversarial probes the gate relies on the engine for", async () => {
  // These are refused by the AST allowlist before execution, but confirm the
  // catalog query the allowlist builds its deny set from still works, and the
  // recursive CTE and generate_series paths are bounded by interrupt and memory.
  const names = await rows(
    connection,
    "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table'",
  );
  const list = new Set(names.map((r) => String(r.function_name).toLowerCase()));
  for (const name of ["range", "generate_series", "read_text", "read_json", "query_table", "unnest"])
    must(list.has(name) || name === "unnest", `${name} is in the table function catalog`);
  must(list.has("unnest"), "unnest in the table function catalog (SELECT unnest(range()) bypass)");
  const worker = await instance.connect();
  const started = Date.now();
  const recursive = worker.runAndReadAll(
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
  );
  setTimeout(() => worker.interrupt(), 200);
  must(await refused(() => recursive), "unbounded recursive CTE stopped");
  worker.closeSync();
  return `${list.size} table functions; unbounded recursive CTE stopped in ${Date.now() - started} ms`;
});

await check("file-free materialization: appender and typed inserts work under lockdown", async () => {
  const writer = await instance.connect();
  await writer.run("CREATE TABLE m(a BIGINT, b VARCHAR, c DOUBLE, d JSON)");
  const appender = await writer.createAppender("m");
  appender.appendBigInt(1n);
  appender.appendVarchar("x");
  appender.appendDouble(1.5);
  appender.appendVarchar('{"k":1}');
  appender.endRow();
  appender.flushSync();
  appender.closeSync();
  const prepared = await writer.prepare("INSERT INTO m VALUES ($1, $2, $3, $4)");
  prepared.bindBigInt(1, 2n);
  prepared.bindVarchar(2, "y");
  prepared.bindDouble(3, 2.5);
  prepared.bindVarchar(4, "[]");
  await prepared.run();
  must(Number(await scalar(writer, "SELECT count(*) FROM m")) === 2, "both rows landed");
  must(await refused(() => writer.runAndReadAll(`SELECT * FROM read_json('${jsonFile}')`)), "read_json stays blocked");
  writer.closeSync();
  return "lockdown does not block in-process writes";
});

function quote(sql) {
  return `'${sql.replaceAll("'", "''")}'`;
}

// AST dump for the comparison against Python 1.5.5.
const statements = JSON.parse(await readFile(join(here, "statements.json"), "utf8"));
const dump = { engine, statements: {} };
const astConnection = await (await DuckDBInstance.create(":memory:")).connect();
await astConnection.run("CREATE TABLE t(a INTEGER, b INTEGER, j JSON)");
await astConnection.run("CREATE TABLE u(a INTEGER)");
for (const sql of statements) {
  let document;
  try {
    document = JSON.parse(
      await scalar(astConnection, `SELECT json_serialize_sql(${quote(sql)})`),
    );
  } catch (error) {
    document = { raised: "Error" };
  }
  let types;
  try {
    const extracted = await astConnection.extractStatements(sql);
    types = [];
    for (let index = 0; index < extracted.count; index++) {
      try {
        types.push(StatementType[(await extracted.prepare(index)).statementType]);
      } catch {
        types.push("PREPARE_REFUSED");
      }
    }
  } catch {
    types = { raised: "Error" };
  }
  dump.statements[sql] = { types, ast: document };
}
const tag = `${platform()}-${arch()}`;
await writeFile(join(here, `ast-node-${tag}.json`), JSON.stringify(dump, null, 1));

const report = {
  node: process.version,
  platform: tag,
  package: "@duckdb/node-api 2.0.0-r.1",
  engine,
  libraryVersion: typeof bindingVersion === "function" ? bindingVersion() : undefined,
  checks: results,
};
await writeFile(join(here, `results-${tag}.json`), JSON.stringify(report, null, 2));

console.log(`node ${report.node} ${tag} engine ${engine}`);
for (const r of results)
  console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}  (${r.ms} ms)\n      ${r.detail}`);
const failed = results.filter((r) => !r.ok).length;
console.log(`${results.length - failed} passed, ${failed} failed`);
connection.closeSync();
instance.closeSync();
process.exit(failed ? 1 : 0);
