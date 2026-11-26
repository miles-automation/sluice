import { StatementType, version, type DuckDBConnection, type DuckDBPreparedStatement } from "@duckdb/node-api";

export const SCOPE_PATTERN = /^[0-9a-f]{32}$/u;

export const ALLOWED_TABLE_FUNCTIONS: ReadonlySet<string> = new Set();

export const MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS: ReadonlySet<string> = new Set([
  "array_resize",
  "bitstring",
  "format",
  "json_serialize_plan",
  "list_resize",
  "lpad",
  "nextval",
  "printf",
  "rpad",
  "setseed",
  "sleep_ms",
  "write_log",
]);

export const QUERY_NODE_TYPES: ReadonlySet<string> = new Set(["SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"]);

export const TABLE_REF_TYPES: ReadonlySet<string> = new Set([
  "BASE_TABLE",
  "EMPTY",
  "EXPRESSION_LIST",
  "JOIN",
  "PIVOT",
  "SHOW_REF",
  "SUBQUERY",
  "TABLE_FUNCTION",
]);

const CANARY_TABLE = "sluice_gate_canary";
const CANARY_REFUSALS: readonly string[] = [
  `SELECT * FROM ${CANARY_TABLE}`,
  `WITH c AS (SELECT * FROM ${CANARY_TABLE}) SELECT * FROM c`,
  `WITH RECURSIVE c AS (SELECT * FROM ${CANARY_TABLE} UNION ALL SELECT * FROM c) SELECT * FROM c`,
  `SELECT 1 UNION ALL SELECT * FROM ${CANARY_TABLE}`,
  `SELECT * FROM (SELECT * FROM ${CANARY_TABLE}) s`,
  `SELECT 1 WHERE EXISTS (SELECT * FROM ${CANARY_TABLE})`,
  `SELECT 1 FROM (SELECT 1) a JOIN ${CANARY_TABLE} b ON true`,
];
const CANARY_ADMITTED = `WITH c AS (SELECT * FROM ${CANARY_TABLE}) SELECT * FROM c`;
const CANARY_FUNCTION_REFUSALS: readonly string[] = [
  "SELECT sleep_ms(1)",
  "SELECT pg_get_viewdef(0)",
  "SELECT pg_sleep(0)",
  "SELECT json_serialize_plan('SELECT 1')",
];
const MACRO_QUERY = /\b(?:select|from)\b/iu;
const MACRO_CALL = /"?([A-Za-z_][A-Za-z0-9_]*)"?\s*\(/gu;
const NOT_SELECT_MESSAGE = "Only SELECT statements";

export class QueryRejectedError extends Error {
  override readonly name = "QueryRejectedError";
}

export class GateContractError extends Error {
  override readonly name = "GateContractError";
}

export type AstSerializer = (sql: string) => Promise<unknown>;

export interface GateOptions {
  readonly serialize?: AstSerializer;
}

type Node = { readonly [key: string]: unknown };

interface Context {
  readonly allowed: ReadonlySet<string>;
  readonly evicted: ReadonlySet<string>;
  readonly restricted: ReadonlySet<string>;
}

export function fold(name: string): string {
  return name.replace(/[A-Z]+/gu, (letters) => letters.toLowerCase());
}

function isNode(value: unknown): value is Node {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function describeKind(value: unknown): string {
  if (isNode(value)) {
    return JSON.stringify(value["type"] ?? null);
  }
  return Array.isArray(value) ? "list" : value === null ? "null" : typeof value;
}

function unreadable(what: string): QueryRejectedError {
  return new QueryRejectedError(`the SQL could not be analyzed: ${what}`);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function serializeSql(sql: string, connection: DuckDBConnection): Promise<unknown> {
  const reader = await connection.runAndReadAll("SELECT json_serialize_sql($1)", [sql]);
  const raw = reader.getRows()[0]?.[0];
  if (typeof raw !== "string") {
    throw unreadable("the parser returned no document");
  }
  return JSON.parse(raw);
}

async function countStatements(sql: string, connection: DuckDBConnection): Promise<number> {
  try {
    return (await connection.extractStatements(sql)).count;
  } catch (error) {
    const text = message(error);
    if (/parser error/iu.test(text)) {
      throw new QueryRejectedError(`could not parse the SQL: ${text}`);
    }
    throw new QueryRejectedError("no statement found");
  }
}

async function statementLayer(sql: string, connection: DuckDBConnection): Promise<unknown> {
  const count = await countStatements(sql, connection);
  if (count === 0) {
    throw new QueryRejectedError("no statement found");
  }
  if (count > 1) {
    throw new QueryRejectedError(`${count} statements found; query takes exactly one`);
  }
  let document: unknown;
  try {
    document = await serializeSql(sql, connection);
  } catch (error) {
    if (error instanceof QueryRejectedError) {
      throw error;
    }
    throw unreadable(message(error));
  }
  if (isNode(document) && document["error"] === true) {
    const reason = String(document["error_message"] ?? "");
    if (reason.includes(NOT_SELECT_MESSAGE)) {
      throw new QueryRejectedError("only SELECT is allowed; the statement is not a query");
    }
    throw unreadable(reason);
  }
  return document;
}

export async function check(
  sql: string,
  connection: DuckDBConnection,
  allowedObjects: Iterable<string>,
  evictedObjects: Iterable<string> = [],
  options: GateOptions = {},
): Promise<void> {
  if (typeof sql !== "string") {
    throw new QueryRejectedError("no statement found");
  }
  const parsed = await statementLayer(sql, connection);
  const document = options.serialize === undefined ? parsed : await options.serialize(sql);
  await checkObjects(document, connection, allowedObjects, evictedObjects);
}

export async function prepareSelect(sql: string, connection: DuckDBConnection): Promise<DuckDBPreparedStatement> {
  const extracted = await connection.extractStatements(sql);
  const prepared = await extracted.prepare(0);
  if (prepared.statementType !== StatementType.SELECT) {
    const kind = StatementType[prepared.statementType];
    prepared.destroySync();
    throw new QueryRejectedError(`only SELECT is allowed, got ${kind}`);
  }
  return prepared;
}

async function names(connection: DuckDBConnection, sql: string): Promise<string[][]> {
  const reader = await connection.runAndReadAll(sql);
  return reader.getRows().map((row) => row.map((value) => (value === null ? "" : String(value))));
}

async function restrictedFunctions(connection: DuckDBConnection): Promise<ReadonlySet<string>> {
  const tableFunctions = await names(
    connection,
    "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table'",
  );
  const denied = new Set<string>([
    ...tableFunctions.map(([name = ""]) => fold(name)),
    ...MUTATING_OR_BLOCKING_SCALAR_FUNCTIONS,
  ]);
  const macros = new Map<string, string>();
  for (const [name = "", definition = ""] of await names(
    connection,
    "SELECT function_name, macro_definition FROM duckdb_functions() WHERE function_type = 'macro'",
  )) {
    const key = fold(name);
    macros.set(key, `${macros.get(key) ?? ""} ${definition}`);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const [name, definition] of macros) {
      if (denied.has(name)) {
        continue;
      }
      const calls = [...definition.matchAll(MACRO_CALL)].map((match) => fold(match[1] ?? ""));
      if (MACRO_QUERY.test(definition) || calls.some((call) => denied.has(call))) {
        denied.add(name);
        changed = true;
      }
    }
  }
  return denied;
}

async function checkObjects(
  document: unknown,
  connection: DuckDBConnection,
  allowedObjects: Iterable<string>,
  evictedObjects: Iterable<string>,
): Promise<void> {
  if (!isNode(document)) {
    throw unreadable("the parse is not an object");
  }
  if (document["error"] !== undefined && document["error"] !== false) {
    throw unreadable(String(document["error_message"] ?? "the parser reported an error"));
  }
  const context: Context = {
    allowed: new Set([...allowedObjects].map(fold)),
    evicted: new Set([...evictedObjects].map(fold)),
    restricted: await restrictedFunctions(connection),
  };
  const statements = document["statements"];
  if (!Array.isArray(statements) || statements.length === 0) {
    throw unreadable("no statements in the parse");
  }
  for (const statement of statements) {
    requireQueryNode(isNode(statement) ? statement["node"] : undefined);
  }
  try {
    validate(document, new Set(), context);
  } catch (error) {
    if (error instanceof RangeError) {
      throw unreadable("the statement is nested too deeply");
    }
    throw error;
  }
}

function requireQueryNode(node: unknown): Node {
  if (!isNode(node) || typeof node["type"] !== "string" || !QUERY_NODE_TYPES.has(node["type"])) {
    throw unreadable(`unrecognized query node ${describeKind(node)}`);
  }
  return node;
}

function requireTableRef(node: unknown): void {
  if (!isNode(node) || typeof node["type"] !== "string" || !TABLE_REF_TYPES.has(node["type"])) {
    throw unreadable(`unrecognized table reference ${describeKind(node)}`);
  }
}

function cteBody(value: Node): readonly [Node, string] {
  if (Object.hasOwn(value, "query_node")) {
    return [requireQueryNode(value["query_node"]), "query_node"];
  }
  const query = value["query"];
  if (isNode(query)) {
    return [requireQueryNode(query["node"]), "query"];
  }
  throw unreadable("a CTE has no body the gate can read");
}

function checkQueryPositions(node: Node, kind: unknown): void {
  if (Object.hasOwn(node, "from_table")) {
    requireTableRef(node["from_table"]);
  }
  if (kind === "JOIN") {
    requireTableRef(node["left"]);
    requireTableRef(node["right"]);
  } else if (kind === "PIVOT") {
    requireTableRef(node["source"]);
  } else if (kind === "SET_OPERATION_NODE") {
    const children = node["children"];
    const members = [node["left"], node["right"], ...(Array.isArray(children) ? (children as unknown[]) : [])];
    const present = members.filter((member) => member !== undefined && member !== null);
    if (present.length < 2) {
      throw unreadable("a set operation has no readable members");
    }
    present.forEach(requireQueryNode);
  } else if (kind === "RECURSIVE_CTE_NODE") {
    requireQueryNode(node["left"]);
    requireQueryNode(node["right"]);
  }
  if (Object.hasOwn(node, "subquery")) {
    const subquery = node["subquery"];
    requireQueryNode(isNode(subquery) ? subquery["node"] : undefined);
  }
}

function cteEntries(node: Node): readonly (readonly [string, Node])[] {
  if (!Object.hasOwn(node, "cte_map")) {
    return [];
  }
  const cteMap = node["cte_map"];
  const declared = isNode(cteMap) ? cteMap["map"] : undefined;
  if (!Array.isArray(declared)) {
    throw unreadable("a CTE list has an unrecognized shape");
  }
  return declared.map((entry: unknown) => {
    if (!isNode(entry) || typeof entry["key"] !== "string" || entry["key"] === "" || !isNode(entry["value"])) {
      throw unreadable("a CTE entry has an unrecognized shape");
    }
    return [fold(entry["key"]), entry["value"]] as const;
  });
}

function validateRecursiveCte(node: Node, visible: ReadonlySet<string>, name: string, context: Context): void {
  validate(requireQueryNode(node["left"]), visible, context);
  validate(requireQueryNode(node["right"]), new Set([...visible, name]), context);
  for (const [key, child] of Object.entries(node)) {
    if (key !== "left" && key !== "right") {
      validate(child, visible, context);
    }
  }
}

function checkBaseTable(node: Node, local: ReadonlySet<string>, context: Context): void {
  const name = node["table_name"];
  if (typeof name !== "string" || name === "") {
    throw unreadable("a table reference has no name");
  }
  const schema = node["schema_name"];
  const catalog = node["catalog_name"];
  const parts = [catalog, schema].filter((part) => part !== undefined && part !== null && part !== "").map(String);
  if (parts.length > 0) {
    throw new QueryRejectedError(`schema-qualified tables are not available: ${[...parts, name].join(".")}`);
  }
  const qualified = node["qualified_name"];
  const path = isNode(qualified) ? qualified["path"] : undefined;
  if (path !== undefined && path !== null && !(Array.isArray(path) && path.length === 1 && path[0] === name)) {
    const shown = Array.isArray(path) ? path.map(String).join(".") : String(path);
    throw new QueryRejectedError(`schema-qualified tables are not available: ${shown}`);
  }
  const normalized = fold(name);
  if (!local.has(normalized) && context.evicted.has(normalized)) {
    throw new QueryRejectedError(
      `table '${name}' was evicted by the session retention budget; rerun the source tool call to materialize it again`,
    );
  }
  if (!local.has(normalized) && !context.allowed.has(normalized)) {
    throw new QueryRejectedError(
      `unknown table '${name}'. You can only query tables named in the handles you were given in this conversation.`,
    );
  }
}

function validate(node: unknown, visible: ReadonlySet<string>, context: Context): void {
  if (Array.isArray(node)) {
    for (const child of node) {
      validate(child, visible, context);
    }
    return;
  }
  if (!isNode(node)) {
    return;
  }
  let local = visible;
  for (const [name, value] of cteEntries(node)) {
    const [queryNode, bodyKey] = cteBody(value);
    if (queryNode["type"] === "RECURSIVE_CTE_NODE") {
      validateRecursiveCte(queryNode, local, name, context);
    } else {
      validate(queryNode, local, context);
    }
    for (const [key, child] of Object.entries(value)) {
      if (key !== bodyKey) {
        validate(child, local, context);
      }
    }
    local = new Set([...local, name]);
  }
  const kind = node["type"];
  checkQueryPositions(node, kind);
  if (kind === "BASE_TABLE") {
    checkBaseTable(node, local, context);
  } else if (kind === "TABLE_FUNCTION") {
    const fn = node["function"];
    const name = isNode(fn) ? String(fn["function_name"]) : "?";
    if (!ALLOWED_TABLE_FUNCTIONS.has(name)) {
      throw new QueryRejectedError(`table function not allowed: ${name}`);
    }
  } else if (kind === "FUNCTION") {
    const name = node["function_name"];
    if (typeof name !== "string" || name === "") {
      throw unreadable("a function call has no name");
    }
    if (context.restricted.has(fold(name))) {
      throw new QueryRejectedError(`function not allowed: ${name}`);
    }
  } else if (kind === "SHOW_REF") {
    throw new QueryRejectedError(
      "SHOW and DESCRIBE are not available; the tables you can query are named in the handles you were given",
    );
  }
  for (const [key, child] of Object.entries(node)) {
    if (key !== "cte_map") {
      validate(child, local, context);
    }
  }
}

function contractFailure(sql: string, outcome: string): string {
  return (
    `Sluice's query gate cannot read the parser output of DuckDB ${version()}, so it refuses to run. ` +
    `Probe ${JSON.stringify(sql)}: ${outcome}. Upgrade Sluice or install a DuckDB version it supports.`
  );
}

async function expectRefusal(sql: string, connection: DuckDBConnection, fragment: string): Promise<void> {
  try {
    await check(sql, connection, []);
  } catch (error) {
    if (error instanceof QueryRejectedError && error.message.includes(fragment)) {
      return;
    }
    throw new GateContractError(contractFailure(sql, message(error)));
  }
  throw new GateContractError(contractFailure(sql, "the query was admitted"));
}

export async function verifyParserContract(connection: DuckDBConnection): Promise<void> {
  const document = await serializeSql(`WITH c AS (SELECT * FROM ${CANARY_TABLE}) SELECT * FROM c`, connection);
  const statements = isNode(document) ? document["statements"] : undefined;
  const first: unknown = Array.isArray(statements) ? statements[0] : undefined;
  const node = isNode(first) ? first["node"] : undefined;
  const cteMap = isNode(node) ? node["cte_map"] : undefined;
  const entries = isNode(cteMap) ? cteMap["map"] : undefined;
  const entry: unknown = Array.isArray(entries) ? entries[0] : undefined;
  const value = isNode(entry) ? entry["value"] : undefined;
  const body = isNode(value) ? value["query_node"] : undefined;
  if (!isNode(body) || body["type"] !== "SELECT_NODE") {
    throw new GateContractError(contractFailure(CANARY_ADMITTED, "a CTE body is not at cte_map.map[].value.query_node"));
  }
  for (const sql of CANARY_REFUSALS) {
    await expectRefusal(sql, connection, CANARY_TABLE);
  }
  for (const sql of CANARY_FUNCTION_REFUSALS) {
    await expectRefusal(sql, connection, "function not allowed");
  }
  try {
    await check(CANARY_ADMITTED, connection, [CANARY_TABLE]);
  } catch (error) {
    throw new GateContractError(contractFailure(CANARY_ADMITTED, message(error)));
  }
}
