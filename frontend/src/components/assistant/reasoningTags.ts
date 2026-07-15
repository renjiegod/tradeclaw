// Defensive splitter for inline <think>/<thinking>/<thought>/<reasoning> markup
// that some OpenAI-compatible providers (e.g. MiniMax) bake directly into the
// visible text instead of a dedicated reasoning_content delta. The backend
// (doyoutrade/models/providers/openai_compatible.py) now splits this out at the
// source for new turns, so this only matters for already-persisted messages
// (or any adapter path this repo doesn't control) that still carry raw tags.
const REASONING_TAG_RE = /<\s*(\/?)\s*(?:think(?:ing)?|thought|reasoning)\b[^<>]*>/gi;

export interface ReasoningTagSplit {
  visible: string;
  thinking: string;
}

export function stripReasoningTags(text: string): ReasoningTagSplit {
  if (!text || text.indexOf("<") === -1) {
    return { visible: text ?? "", thinking: "" };
  }
  let visible = "";
  let thinking = "";
  let depth = 0;
  let lastIndex = 0;
  REASONING_TAG_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = REASONING_TAG_RE.exec(text)) !== null) {
    const before = text.slice(lastIndex, match.index);
    if (depth > 0) {
      thinking += before;
    } else {
      visible += before;
    }
    const isClose = match[1] === "/";
    depth = isClose ? Math.max(0, depth - 1) : depth + 1;
    lastIndex = match.index + match[0].length;
  }
  const rest = text.slice(lastIndex);
  if (depth > 0) {
    thinking += rest;
  } else {
    visible += rest;
  }
  return { visible, thinking };
}
