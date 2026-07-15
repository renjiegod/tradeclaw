// frontend/src/components/assistant/types.ts

export type TextBlock = {
  type: "text";
  content: string;
};

export type ThinkingBlock = {
  type: "thinking";
  thinking: string;
};

export type ToolStatus = "pending" | "running" | "completed" | "error";

export type ToolUseBlock = {
  type: "tool_use";
  id: string;
  name: string;
  category?: string;
  input: unknown;
  status: ToolStatus;
};

export type ToolResultBlock = {
  type: "tool_result";
  tool_use_id: string;
  output: unknown;
  is_error: boolean;
};

export type MessageBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock;

export type ToolCallEntry = {
  tool: ToolUseBlock;
  result?: ToolResultBlock;
  attempt_id?: string;
};

export function parseToolResultPreview(
  preview: string | undefined,
  isError: boolean | undefined,
): ToolResultBlock | undefined {
  if (preview === undefined) return undefined;
  let output: unknown = preview;
  let parsedIsError = false;
  try {
    const parsed = JSON.parse(preview);
    output = parsed.result ?? parsed;
    parsedIsError = parsed.status === "error" || parsed.is_error === true;
  } catch {
    // keep string preview
  }
  return {
    type: "tool_result",
    tool_use_id: "",
    output,
    is_error: isError ?? parsedIsError,
  };
}

export function toolStatusFromResult(
  tool: Pick<ToolUseBlock, "status">,
  result?: Pick<ToolResultBlock, "is_error">,
): ToolStatus {
  return result?.is_error ? "error" : tool.status;
}
