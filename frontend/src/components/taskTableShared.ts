import type { TaskStatus, TaskTrigger } from "../types";
import { formatSymbolWithName } from "../hooks/useSymbolNames";

// Re-exported from the shared class-name home so the two task tables keep their
// existing import site while the constants live in one place.
export { PANEL_CARD_CLASSNAME, SOFT_TAG_CLASSNAME } from "../styles/classNames";

/** Shared pure helpers for the trading / backtest task tables.
 *
 * The two list tabs render genuinely different columns (a backtest cares about
 * range / return / drawdown; a trading task cares about its triggers / cycles),
 * but they agree on these formatting primitives, so keep them in one place
 * instead of duplicating drift-prone money/percent logic across components. */

export const MODE_LABEL_MAP: Record<string, string> = {
  paper: "模拟盘",
  live: "实盘",
  backtest: "回测",
  signal_only: "信号",
};

/** Modes that belong to the "交易任务" tab (everything that is not a backtest). */
export const TRADING_MODES = ["paper", "live", "signal_only"] as const;

export function formatMode(mode: string): string {
  return MODE_LABEL_MAP[mode] ?? mode;
}

export function readDefinitionId(settings: Record<string, unknown> | null): string | null {
  const strategy = settings?.strategy as Record<string, unknown> | undefined;
  const id = strategy?.definition_id;
  return typeof id === "string" && id.trim() ? id : null;
}

export function formatUniverse(
  universe: string[],
  symbolNames: Record<string, string | null>,
): string {
  if (!universe?.length) return "—";
  const first = formatSymbolWithName(universe[0]!, symbolNames);
  if (universe.length === 1) return first;
  return `${first} 等${universe.length}只`;
}

export function formatSignedPct(
  value: string | number | null | undefined,
  digits = 2,
): string {
  if (value == null) return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}%`;
}

/** A股 convention: gains are red, losses are green. Undefined / zero → no color. */
export function returnPctColor(
  value: string | number | null | undefined,
): string | undefined {
  if (value == null) return undefined;
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "#cf1322" : "#3f8600";
}

export function universeSymbolsOf(tasks: TaskStatus[]): string[] {
  return Array.from(new Set(tasks.flatMap((task) => task.universe ?? [])));
}

export function sortByCreatedDesc(tasks: TaskStatus[]): TaskStatus[] {
  return [...tasks].sort(
    (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
  );
}

/** Per-task trigger rollup shown as a chip in the trading-task list.
 *
 * ``total`` counts every owned trigger; ``active`` counts the schedulable ones
 * (enabled + status active). ``nextFireAt`` is the soonest upcoming fire across
 * the active triggers (ISO string, naive-UTC as the API returns it), used to
 * preview "下次 …" without opening the task. */
export type TriggerSummary = {
  total: number;
  active: number;
  nextFireAt: string | null;
};

export function summarizeTriggers(triggers: TaskTrigger[]): TriggerSummary {
  let active = 0;
  let nextFireAt: string | null = null;
  for (const trg of triggers) {
    const isActive = trg.enabled && trg.status === "active";
    if (isActive) active += 1;
    if (isActive && trg.next_fire_at) {
      if (nextFireAt == null || Date.parse(trg.next_fire_at) < Date.parse(nextFireAt)) {
        nextFireAt = trg.next_fire_at;
      }
    }
  }
  return { total: triggers.length, active, nextFireAt };
}
