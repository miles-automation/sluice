import type { Case, Kind } from "./fixtures.ts";

export type Adapter = (testCase: Case) => void | Promise<void>;

export interface Pending {
  readonly pending: string;
}

export const adapters: Readonly<Record<Kind, Adapter | Pending>> = {
  extract: { pending: "task 1076 (shape)" },
  project: { pending: "task 1076 (shape)" },
  infer: { pending: "task 1076 (infer)" },
  naming: { pending: "task 1076 (naming)" },
  scope: { pending: "task 1076 (scope)" },
  cell: { pending: "task 1077 (query rendering)" },
  gate: { pending: "task 1077 (gate)" },
  gate_ast: { pending: "task 1077 (gate)" },
  engine: { pending: "task 1077 (DuckDB binding)" },
  session: { pending: "task 1077 (store, gate and query)" },
  paginate: { pending: "task 1078 (pagination)" },
  config: { pending: "task 1078 (CLI and config)" },
};
