import { isJsonArray, isJsonObject, type JsonArray, type JsonObject, type JsonValue } from "./json.ts";

export const ROW_COLUMN = "_row";
export const CALL_COLUMN = "_call_id";
export const EXTRA_COLUMN = "_extra";
export const VALUE_COLUMN = "value";

export const RESERVED: readonly string[] = [ROW_COLUMN, CALL_COLUMN, EXTRA_COLUMN];

export interface RowSet {
  readonly sourcePath: string;
  readonly rows: readonly JsonObject[];
}

export type Extraction =
  | { readonly rowSets: readonly RowSet[]; readonly reason: null }
  | { readonly rowSets: readonly []; readonly reason: string };

export type ProjectedRecord = ReadonlyMap<string, JsonValue>;

export interface Projection {
  readonly columns: readonly string[];
  readonly records: readonly ProjectedRecord[];
  readonly renamed: ReadonlyMap<string, string>;
  readonly extraKeys: readonly string[];
  readonly hasExtra: boolean;
}

function allObjects(items: JsonArray): items is readonly JsonObject[] {
  return items.length > 0 && items.every((item) => isJsonObject(item));
}

function allScalars(items: JsonArray): boolean {
  return items.length > 0 && items.every((item) => !isJsonObject(item) && !isJsonArray(item));
}

function pythonTypeName(value: JsonValue): string {
  switch (typeof value) {
    case "bigint":
      return "int";
    case "number":
      return "float";
    case "string":
      return "str";
    case "boolean":
      return "bool";
    default:
      return value === null ? "NoneType" : "object";
  }
}

function found(rowSets: readonly RowSet[]): Extraction {
  return { rowSets, reason: null };
}

function refused(reason: string): Extraction {
  return { rowSets: [], reason };
}

function paths(keys: readonly string[]): string {
  return keys.map((key) => `$.${key}`).join(", ");
}

function extractObject(payload: JsonObject): Extraction {
  const candidates: RowSet[] = [];
  const ambiguous: string[] = [];
  const empty: string[] = [];
  for (const [key, value] of payload) {
    if (!isJsonArray(value)) {
      continue;
    }
    if (allObjects(value)) {
      candidates.push({ sourcePath: `$.${key}`, rows: value });
    } else if (value.some((item) => isJsonObject(item))) {
      ambiguous.push(key);
    } else if (value.length === 0) {
      empty.push(key);
    }
  }
  if (candidates.length > 0) {
    return found(candidates);
  }
  if (ambiguous.length > 0) {
    return refused(`mixed_elements: ${paths(ambiguous)} mixes objects and non-objects`);
  }
  if (empty.length > 0) {
    return refused(`empty: ${paths(empty)} had 0 rows`);
  }
  return found([{ sourcePath: "$", rows: [payload] }]);
}

export function extract(payload: JsonValue): Extraction {
  if (isJsonArray(payload)) {
    if (payload.length === 0) {
      return refused("empty: the payload is an empty list");
    }
    if (allObjects(payload)) {
      return found([{ sourcePath: "$", rows: payload }]);
    }
    if (allScalars(payload)) {
      return found([{ sourcePath: "$", rows: payload.map((item) => new Map([[VALUE_COLUMN, item]])) }]);
    }
    return refused("mixed_elements: the list mixes objects and non-objects");
  }
  if (isJsonObject(payload)) {
    return extractObject(payload);
  }
  return refused(`scalar: the payload is a bare ${pythonTypeName(payload)}`);
}

function storedName(key: string, taken: ReadonlySet<string>): string {
  if (!RESERVED.includes(key) && !taken.has(key)) {
    return key;
  }
  let candidate = `${key}__src`;
  let suffix = 2;
  while (taken.has(candidate) || RESERVED.includes(candidate)) {
    candidate = `${key}__src${suffix}`;
    suffix += 1;
  }
  return candidate;
}

export function project(rows: readonly JsonObject[], maxColumns: number): Projection {
  if (!Number.isSafeInteger(maxColumns) || maxColumns < 0) {
    throw new RangeError(`maxColumns must be a non-negative integer, got ${maxColumns}`);
  }
  const presence = new Map<string, number>();
  for (const row of rows) {
    for (const key of row.keys()) {
      presence.set(key, (presence.get(key) ?? 0) + 1);
    }
  }
  const order = new Map([...presence.keys()].map((key, index) => [key, index]));
  const rank = (key: string): number => order.get(key) ?? 0;
  const ranked = [...presence.keys()].sort((a, b) => (presence.get(b) ?? 0) - (presence.get(a) ?? 0));
  const kept = ranked.slice(0, maxColumns).sort((a, b) => rank(a) - rank(b));
  const extraKeys = ranked.slice(maxColumns);

  const renamed = new Map<string, string>();
  const taken = new Set<string>();
  const stored = new Map<string, string>();
  for (const key of kept) {
    const name = storedName(key, taken);
    taken.add(name);
    stored.set(key, name);
    if (name !== key) {
      renamed.set(key, name);
    }
  }

  const columns = kept.map((key) => stored.get(key) ?? key);
  if (extraKeys.length > 0) {
    columns.push(EXTRA_COLUMN);
  }

  const records = rows.map((row, index) => {
    const record = new Map<string, JsonValue>([[ROW_COLUMN, BigInt(index)]]);
    for (const key of kept) {
      record.set(stored.get(key) ?? key, row.get(key) ?? null);
    }
    if (extraKeys.length > 0) {
      const leftover = new Map<string, JsonValue>();
      for (const key of extraKeys) {
        const value = row.get(key);
        if (value !== undefined) {
          leftover.set(key, value);
        }
      }
      record.set(EXTRA_COLUMN, leftover.size > 0 ? leftover : null);
    }
    return record;
  });

  return { columns, records, renamed, extraKeys, hasExtra: extraKeys.length > 0 };
}
