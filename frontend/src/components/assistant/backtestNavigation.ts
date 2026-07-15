// frontend/src/components/assistant/backtestNavigation.ts
//
// Shared helpers for surfacing a "jump to backtest task" affordance in the
// chat UI. Two callers consume these:
//
//   1. ``InlineToolCallCard`` — attaches the button to the tool card itself
//      so the affordance appears the moment ``run_strategy_backtest`` /
//      ``get_backtest_summary`` resolves.
//   2. ``MessageContentRenderer`` — attaches a duplicate button below the
//      assistant's prose report. The report is plain markdown text and
//      arrives as a sibling block to the tool card, so once the user
//      scrolls past the card the affordance would otherwise disappear.
//      A second button anchored at the message footer keeps it reachable.
//
// Both call sites need exactly the same task_id extraction logic — keeping
// it here means the two surfaces never drift.

/** Tool names whose results we treat as "produced a backtest the user might
 * want to inspect on the task detail page". ``get_backtest_summary`` is
 * included so reading a past backtest's persisted summary also surfaces
 * the jump. */
export const BACKTEST_NAV_TOOLS = new Set<string>([
  "run_strategy_backtest",
  "get_backtest_summary",
]);

/** Matches ``doyoutrade-cli backtest <subcmd>`` anywhere in a shell command
 * string — assistant commands sometimes get chained with ``&&`` / ``;`` /
 * ``|`` so anchoring to start is too strict. ``[\s;&|]`` plus ``^`` covers
 * those framings without false-matching ``my-doyoutrade-cli-backtest`` style
 * substrings. */
const BACKTEST_CLI_RE = /(?:^|[\s;&|])doyoutrade-cli\s+backtest\b/;

function isBacktestExecuteBashCall(args: Record<string, unknown> | undefined): boolean {
  if (!args) return false;
  const cmd = args["command"];
  return typeof cmd === "string" && BACKTEST_CLI_RE.test(cmd);
}

/** A tool call produces a "jump to task detail" affordance when it's either
 * one of the native backtest tools, or an ``execute_bash`` invoking the
 * ``doyoutrade-cli backtest`` CLI (the path most agent flows use now that
 * backtests run through the CLI envelope instead of the in-process tool). */
export function isBacktestProducingToolCall(block: {
  name?: string;
  arguments?: Record<string, unknown>;
}): boolean {
  if (!block.name) return false;
  if (BACKTEST_NAV_TOOLS.has(block.name)) return true;
  if (block.name === "execute_bash") return isBacktestExecuteBashCall(block.arguments);
  return false;
}

/** Extract ``task_id`` from a serialized tool-result payload.
 *
 * The tool output reaches the chat in one of three shapes:
 *   - a parsed object (the chat layer already JSON-decoded the envelope)
 *   - the raw envelope string ``"<prose>\n\n```json\n{...}\n```"``
 *   - a truncated / malformed string where neither parse path applies
 *
 * Returns ``null`` for the last case so callers can hide the affordance
 * silently instead of showing a broken link.
 */
function extractTaskIdFromObject(obj: Record<string, unknown>): string | null {
  for (const field of ["task_id", "auto_created_task_id"]) {
    const val = obj[field];
    if (typeof val === "string" && val) return val;
  }
  for (const nest of ["backtest_job", "run"]) {
    const inner = obj[nest];
    if (inner && typeof inner === "object") {
      const t = (inner as Record<string, unknown>)["task_id"];
      if (typeof t === "string" && t) return t;
    }
  }
  // CLI envelope shape: ``{ok, data, meta}`` — descend into ``data`` so
  // ``doyoutrade-cli backtest <subcmd>`` outputs (which nest the real
  // payload under ``data``) are recognized end-to-end.
  const data = obj["data"];
  if (data && typeof data === "object") {
    return extractTaskIdFromObject(data as Record<string, unknown>);
  }
  return null;
}

export function extractTaskIdFromToolResult(output: unknown): string | null {
  if (output == null) return null;
  if (typeof output === "object") {
    return extractTaskIdFromObject(output as Record<string, unknown>);
  }
  if (typeof output !== "string") return null;
  // Prefer parsing the fenced ```json ... ``` envelope first since the
  // outer prose can carry stray quoted strings that would trip up the
  // bare regex below.
  const fence = output.match(/```json\s*([\s\S]*?)```/);
  const jsonChunk = fence ? fence[1] : output;
  try {
    const parsed = JSON.parse(jsonChunk);
    const t = extractTaskIdFromToolResult(parsed);
    if (t) return t;
  } catch {
    // fall through
  }
  const m = output.match(/"task_id"\s*:\s*"([^"]+)"/);
  return m ? m[1] : null;
}

/** Walk a list of content blocks (the same shape ``MessageContentRenderer``
 * receives) and return the most-recent recoverable ``task_id`` from a
 * backtest-producing tool call. Returns ``null`` when no such block exists
 * or the task_id can't be extracted from either side of the call
 * (arguments / result_preview).
 *
 * Implementation note: we iterate forwards and keep overwriting because
 * the *latest* call is the right one to link — an agent can re-run the
 * same task during a single turn and we want the freshest result.
 */
export function findBacktestTaskIdInBlocks(
  blocks: Array<
    | { type: "thinking"; content: string }
    | {
        type: "tool_call";
        name?: string;
        arguments?: Record<string, unknown>;
        result_preview?: string;
        is_error?: boolean;
        status?: string;
      }
    | { type: "text"; content: string }
  >,
): string | null {
  let latest: string | null = null;
  for (const block of blocks) {
    if (block.type !== "tool_call") continue;
    if (!isBacktestProducingToolCall(block)) continue;
    if (block.is_error) continue;
    if (block.status && block.status !== "completed") continue;
    // ``task_id`` from arguments wins when present — it survives even when
    // the JSON envelope in ``result_preview`` got truncated by the per-
    // agent ``tool_result_max_chars`` budget. Skipped for execute_bash
    // (whose ``arguments.task_id`` would point at a background bash task,
    // not a backtest task).
    if (block.name !== "execute_bash") {
      const argId = block.arguments?.task_id;
      if (typeof argId === "string" && argId) {
        latest = argId;
        continue;
      }
    }
    if (block.result_preview) {
      const fromResult = extractTaskIdFromToolResult(block.result_preview);
      if (fromResult) latest = fromResult;
    }
  }
  return latest;
}
