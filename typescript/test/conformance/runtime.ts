import assert from "node:assert/strict";
import type { Json } from "./fixtures.ts";
import { matchCell } from "./matchers.ts";

export const LOCKDOWN: readonly string[] = [
  "SET enable_external_access = false",
  "SET autoinstall_known_extensions = false",
  "SET autoload_known_extensions = false",
  "SET allow_community_extensions = false",
  "SET lock_configuration = true",
];

const GATE_CATEGORIES: readonly (readonly [string, string])[] = [
  ["could not parse", "parse_error"],
  ["no statement found", "no_statement"],
  ["statements found", "multiple_statements"],
  ["only SELECT is allowed", "not_select"],
  ["could not be analyzed", "unanalyzable"],
  ["table function not allowed", "table_function"],
  ["function not allowed", "function"],
  ["schema-qualified", "schema_qualified"],
  ["was evicted", "evicted_table"],
  ["unknown table", "unknown_table"],
  ["SHOW and DESCRIBE", "show_describe"],
  ["max_rows must be", "invalid_argument"],
  ["needs a `sql` string", "invalid_argument"],
  ["timeout", "timeout"],
];

export function rejectionCategory(message: string): string {
  for (const [fragment, category] of GATE_CATEGORIES) {
    if (message.includes(fragment)) {
      return category;
    }
  }
  return "execution_error";
}

type JsonRecord = { readonly [key: string]: Json };

function strings(value: Json | undefined): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

export function assertRejection(expect: JsonRecord, message: string): void {
  const category = rejectionCategory(message);
  assert.ok(strings(expect["categories"]).includes(category), `category ${category} from ${JSON.stringify(message)}`);
  const folded = message.toLowerCase();
  for (const fragment of strings(expect["mentions"])) {
    assert.ok(folded.includes(fragment.toLowerCase()), `${fragment} not named in ${message}`);
  }
  for (const fragment of strings(expect["not_mentions"])) {
    assert.ok(!folded.includes(fragment.toLowerCase()), `${fragment} leaked in ${message}`);
  }
}

export interface QueryView {
  columns: string[];
  rows: string[][];
  notices: string[];
  shown: number;
  more_rows: boolean;
  rows_omitted: number;
  cells_truncated: number;
  headers_truncated: number;
  table_omitted: boolean;
}

const NOTICES: readonly (readonly [string, RegExp])[] = [
  ["shown", /^([0-9]+) row\(s\) shown\./u],
  ["more_rows", /^Additional rows exist beyond max_rows; the exact number is not known\./u],
  ["rows_omitted", /^([0-9]+) row\(s\) omitted to stay under the byte cap\./u],
  ["cells_truncated", /^([0-9]+) cell\(s\) truncated, marked with a trailing …\./u],
  ["headers_truncated", /^([0-9]+) column header\(s\) truncated, marked with a trailing …\./u],
  ["table_omitted", /^The table was omitted because its header exceeded the output byte cap\./u],
  ["fallback", /^Output truncated to stay under the configured byte cap\./u],
];

function splitRow(line: string): string[] {
  assert.ok(line.startsWith("| ") && line.endsWith(" |"), line);
  return line.slice(2, -2).split(" | ");
}

function parseNotices(line: string): (readonly [string, number])[] {
  const found: (readonly [string, number])[] = [];
  let rest = line;
  for (const [kind, pattern] of NOTICES) {
    const match = pattern.exec(rest);
    if (match === null) {
      continue;
    }
    found.push([kind, match[1] === undefined ? 1 : Number(match[1])]);
    rest = rest.slice(match[0].length);
    if (rest.startsWith(" ")) {
      rest = rest.slice(1);
    }
  }
  assert.equal(rest, "", `unrecognized notice text ${JSON.stringify(rest)} in ${JSON.stringify(line)}`);
  assert.ok(
    found.length > 0 && (found[0]?.[0] === "shown" || (found.length === 1 && found[0]?.[0] === "fallback")),
    line,
  );
  return found;
}

export function parseQueryOutput(text: string): QueryView {
  const view: QueryView = {
    columns: [],
    rows: [],
    notices: [],
    shown: 0,
    more_rows: false,
    rows_omitted: 0,
    cells_truncated: 0,
    headers_truncated: 0,
    table_omitted: false,
  };
  const split = text.lastIndexOf("\n\n");
  const body = split === -1 ? "" : text.slice(0, split);
  const notice = split === -1 ? text : text.slice(split + 2);
  const lines = split === -1 ? [] : body.split("\n");
  const table = lines.filter((line) => line.startsWith("| "));
  assert.ok(
    table.length === lines.length || (lines.length === 1 && lines[0] === "(no columns)"),
    `unexpected body ${JSON.stringify(body)}`,
  );
  if (table.length > 0) {
    assert.ok(table.length >= 2, String(table));
    view.columns = splitRow(table[0] ?? "");
    view.rows = table.slice(2).map(splitRow);
  }
  for (const [kind, count] of parseNotices(notice)) {
    view.notices.push(kind);
    switch (kind) {
      case "fallback":
        break;
      case "more_rows":
        view.more_rows = true;
        break;
      case "table_omitted":
        view.table_omitted = true;
        break;
      case "shown":
        view.shown = count;
        break;
      case "rows_omitted":
        view.rows_omitted = count;
        break;
      case "cells_truncated":
        view.cells_truncated = count;
        break;
      case "headers_truncated":
        view.headers_truncated = count;
        break;
    }
  }
  return view;
}

export function assertQueryView(expect: JsonRecord, view: QueryView): void {
  if ("columns" in expect) {
    assert.deepEqual(view.columns, expect["columns"]);
  }
  if ("rows" in expect) {
    const rows = expect["rows"];
    assert.ok(Array.isArray(rows));
    assert.equal(view.rows.length, rows.length, JSON.stringify(view.rows));
    rows.forEach((expected, index) => {
      const row = view.rows[index] ?? [];
      assert.ok(Array.isArray(expected));
      assert.equal(row.length, expected.length, JSON.stringify(row));
      expected.forEach((matcher, column) => {
        const cell = row[column] ?? "";
        assert.ok(matchCell(matcher, cell), `cell ${JSON.stringify(cell)} does not match ${JSON.stringify(matcher)}`);
      });
    });
  }
  if ("row_count" in expect) {
    assert.equal(view.rows.length, expect["row_count"]);
  }
  for (const key of ["shown", "more_rows", "rows_omitted", "cells_truncated", "headers_truncated"] as const) {
    if (key in expect) {
      assert.equal(view[key], expect[key], `${key}: ${String(view[key])}`);
    }
  }
  if ("notices" in expect) {
    assert.deepEqual(view.notices, expect["notices"]);
  }
  if ("accounted_rows" in expect) {
    assert.equal(view.shown + view.rows_omitted, expect["accounted_rows"]);
  }
  if ("rows_omitted_min" in expect) {
    assert.ok(view.rows_omitted >= Number(expect["rows_omitted_min"]));
  }
  if ("table_omitted" in expect) {
    assert.equal(view.table_omitted, expect["table_omitted"]);
  }
  if (expect["headers_truncated_or_table_omitted"] === true) {
    assert.ok(view.headers_truncated > 0 || view.table_omitted);
  }
}
