/** Heuristic labels for request body shape (not authoritative provider). */
export const LABEL_ANTHROPIC_SHAPE = "Anthropic 形";
export const LABEL_OPENAI_SHAPE = "OpenAI 形";

export type ModelInvocationRequestPreview =
  | { kind: "none" }
  | { kind: "markdown"; protocolLabel: string; text: string }
  | { kind: "notice"; protocolLabel: string; message: string };

/**
 * Infer Anthropic Messages-style (top-level string `system`) vs OpenAI Chat-style
 * (`messages` with `role: "system"`) and extract the first system string for Markdown preview.
 */
export function analyzeModelInvocationRequest(request: unknown): ModelInvocationRequestPreview {
  if (request == null || typeof request !== "object" || Array.isArray(request)) {
    return { kind: "none" };
  }
  const r = request as Record<string, unknown>;

  if (Object.prototype.hasOwnProperty.call(r, "system")) {
    const s = r["system"];
    if (typeof s === "string") {
      if (s.length > 0) {
        return { kind: "markdown", protocolLabel: LABEL_ANTHROPIC_SHAPE, text: s };
      }
      return { kind: "none" };
    }
    if (s !== undefined) {
      return {
        kind: "notice",
        protocolLabel: LABEL_ANTHROPIC_SHAPE,
        message: "系统提示词无法预览（system 非字符串），请查看下方 JSON。",
      };
    }
  }

  const messages = r["messages"];
  if (!Array.isArray(messages)) {
    return { kind: "none" };
  }

  for (const m of messages) {
    if (m == null || typeof m !== "object" || Array.isArray(m)) continue;
    const msg = m as Record<string, unknown>;
    const role = typeof msg["role"] === "string" ? msg["role"].toLowerCase() : "";
    if (role !== "system") continue;

    const c = msg["content"];
    if (typeof c === "string") {
      if (c.length > 0) {
        return { kind: "markdown", protocolLabel: LABEL_OPENAI_SHAPE, text: c };
      }
      return { kind: "none" };
    }
    return {
      kind: "notice",
      protocolLabel: LABEL_OPENAI_SHAPE,
      message: "系统提示词无法预览（content 非字符串），请查看下方 JSON。",
    };
  }

  return { kind: "none" };
}
