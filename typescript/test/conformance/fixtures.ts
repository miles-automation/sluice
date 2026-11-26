import { readFileSync, readdirSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";

export const KINDS = [
  "extract",
  "project",
  "infer",
  "naming",
  "scope",
  "cell",
  "gate",
  "gate_ast",
  "engine",
  "session",
  "paginate",
  "config",
] as const;

export type Kind = (typeof KINDS)[number];

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Case {
  readonly id: string;
  readonly [field: string]: Json;
}

export interface Fixture {
  readonly file: string;
  readonly kind: Kind;
  readonly description: string;
  readonly cases: readonly Case[];
}

export const CONFORMANCE_ROOT = new URL("../../../conformance/", import.meta.url);
const FIXTURES = new URL("fixtures/", CONFORMANCE_ROOT);

function readJson(url: URL): unknown {
  return JSON.parse(readFileSync(url, "utf8"));
}

function isKind(value: unknown): value is Kind {
  return typeof value === "string" && (KINDS as readonly string[]).includes(value);
}

export function loadSchemaValidator(): (document: unknown) => string[] {
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  const validate = ajv.compile(readJson(new URL("schema.json", CONFORMANCE_ROOT)) as object);
  return (document) =>
    validate(document)
      ? []
      : (validate.errors ?? []).map((error) => `${error.instancePath || "/"}: ${error.message ?? "invalid"}`);
}

export function loadFixtures(): Fixture[] {
  const validate = loadSchemaValidator();
  const seen = new Map<string, string>();
  const fixtures: Fixture[] = [];
  const files = readdirSync(FIXTURES)
    .filter((name) => name.endsWith(".json"))
    .sort();
  if (files.length === 0) {
    throw new Error(`no fixtures found under ${FIXTURES.pathname}`);
  }
  for (const file of files) {
    const document = readJson(new URL(file, FIXTURES));
    const errors = validate(document);
    if (errors.length > 0) {
      throw new Error(`${file} does not match conformance/schema.json: ${errors.slice(0, 5).join("; ")}`);
    }
    const { kind, description, defaults, cases } = document as {
      kind: unknown;
      description: string;
      defaults?: Record<string, Json>;
      cases: Case[];
    };
    if (!isKind(kind)) {
      throw new Error(`${file} has unknown kind ${String(kind)}`);
    }
    for (const testCase of cases) {
      const previous = seen.get(testCase.id);
      if (previous !== undefined) {
        throw new Error(`duplicate case id ${testCase.id} in ${file} and ${previous}`);
      }
      seen.set(testCase.id, file);
    }
    fixtures.push({
      file,
      kind,
      description,
      cases: cases.map((testCase) => ({ ...defaults, ...testCase })),
    });
  }
  return fixtures;
}

export function oneSided(testCase: Case): string | undefined {
  const only = testCase["only"];
  if (typeof only !== "object" || only === null || Array.isArray(only) || only["version"] === "typescript") {
    return undefined;
  }
  return `one-sided, ${String(only["version"])} only: ${String(only["reason"])}`;
}
