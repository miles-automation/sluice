import type { ToolResult } from "./models.ts";

export const FailureClass = {
  TOOL_ERROR: "tool_error",
  OUTPUT_SCHEMA: "output_schema",
  PROTOCOL: "protocol",
  TRANSPORT: "transport",
} as const;

export type FailureClass = (typeof FailureClass)[keyof typeof FailureClass];

export class DownstreamError extends Error {
  override readonly name = "DownstreamError";
  readonly failureClass: FailureClass;
  readonly tool: string;
  readonly detail: string;

  constructor(failureClass: FailureClass, tool: string, detail: string) {
    super(`${failureClass}: ${tool}: ${detail}`);
    this.failureClass = failureClass;
    this.tool = tool;
    this.detail = detail;
  }
}

export function errorResult(failure: DownstreamError): ToolResult {
  return {
    content: [
      {
        type: "text",
        text: `sluice: downstream call failed (${failure.failureClass}).\ntool: ${failure.tool}\n${failure.detail}`,
      },
    ],
    isError: true,
  };
}
