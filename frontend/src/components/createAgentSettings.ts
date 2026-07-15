import type { DataCacheSource, TaskStatus } from "../types";

/** Pure settings ⇆ form coercion helpers for {@link CreateAgentCard}.
 *
 * Extracted from the 1000-line component so the form body stays focused on
 * rendering / wiring. These are deliberately side-effect free: each reads a
 * (possibly hand-edited, possibly missing) ``settings`` blob and returns the
 * value the form should show, falling back to the backend defaults captured in
 * {@link DEFAULT_SIGNAL_FORM}. Keep them pure so they remain trivially testable
 * and reusable from the settings JSON editor. */

export const DEFAULT_SIGNAL_FORM = {
  universe_symbols: [] as string[],
  /** ``null`` = no per-order cap (matches backend default). */
  max_single_order_amount: null as number | null,
  review_equity_fraction: 1,
  max_position_ratio: 0.3,
  /** 1 = whole-share trading (backend default); A股 grids set 100. */
  lot_size: 1,
  /** 0 = no rebalance dead band (backend default). */
  rebalance_hysteresis_lots: 0,
  /** null = no task-level total position amount cap. */
  max_task_position_amount: null as number | null,
  /** null = no task-level total position ratio cap. */
  max_task_position_ratio: null as number | null,
  /** 单笔计划名义金额达到该值时进入人工审批。 */
  min_notional_for_approval: 1000,
  /** 审批超时（秒）。 */
  approval_timeout_seconds: 300,
  /** Backtest debug observability toggle; true = full trace (default). */
  backtest_debug_enabled: true,
};

export function defaultSettingsObject(): Record<string, unknown> {
  return {};
}

export function initialSettingsFromTask(edit: TaskStatus): Record<string, unknown> {
  const s = edit.settings;
  if (s && typeof s === "object" && !Array.isArray(s)) {
    return structuredClone(s as Record<string, unknown>);
  }
  return defaultSettingsObject();
}

export function normalizeOptionalText(value?: string): string | undefined {
  const normalized = value?.trim();
  return normalized ? normalized : undefined;
}

export function maxSingleOrderAmountFromSettings(
  settings: Record<string, unknown> | null | undefined,
): number | null {
  const blocks = [settings?.agent?.position_constraints, settings?.position_constraints];
  for (const pc of blocks) {
    if (pc && typeof pc === "object" && !Array.isArray(pc)) {
      const raw = (pc as Record<string, unknown>).max_single_order_amount;
      if (raw === null) {
        return null;
      }
      if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
        return raw;
      }
      if (typeof raw === "string" && raw.trim()) {
        const n = Number.parseFloat(raw);
        if (Number.isFinite(n) && n > 0) {
          return n;
        }
      }
    }
  }
  return DEFAULT_SIGNAL_FORM.max_single_order_amount;
}

export function reviewEquityFractionFromSettings(settings: Record<string, unknown> | null | undefined): number {
  const blocks = [settings?.agent?.position_constraints, settings?.position_constraints];
  for (const pc of blocks) {
    if (pc && typeof pc === "object" && !Array.isArray(pc)) {
      const raw = (pc as Record<string, unknown>).review_equity_fraction;
      if (typeof raw === "number" && Number.isFinite(raw)) {
        return raw;
      }
      if (typeof raw === "string" && raw.trim()) {
        const n = Number.parseFloat(raw);
        if (Number.isFinite(n)) {
          return n;
        }
      }
    }
  }
  return DEFAULT_SIGNAL_FORM.review_equity_fraction;
}

export function maxPositionRatioFromSettings(settings: Record<string, unknown> | null | undefined): number {
  const blocks = [settings?.agent?.position_constraints, settings?.position_constraints];
  for (const pc of blocks) {
    if (pc && typeof pc === "object" && !Array.isArray(pc)) {
      const raw = (pc as Record<string, unknown>).max_position_ratio;
      if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
        return raw;
      }
      if (typeof raw === "string" && raw.trim()) {
        const n = Number.parseFloat(raw);
        if (Number.isFinite(n) && n > 0) {
          return n;
        }
      }
    }
  }
  return 0.3; // DEFAULT_POSITION_RATIO
}

function optionalPositionConstraintNumberFromSettings(
  settings: Record<string, unknown> | null | undefined,
  key: "max_task_position_amount" | "max_task_position_ratio",
): number | null {
  const blocks = [settings?.agent?.position_constraints, settings?.position_constraints];
  for (const pc of blocks) {
    if (pc && typeof pc === "object" && !Array.isArray(pc)) {
      const raw = (pc as Record<string, unknown>)[key];
      if (raw === null) {
        return null;
      }
      if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
        return raw;
      }
      if (typeof raw === "string" && raw.trim()) {
        const n = Number.parseFloat(raw);
        if (Number.isFinite(n) && n > 0) {
          return n;
        }
      }
    }
  }
  return null;
}

export function maxTaskPositionAmountFromSettings(
  settings: Record<string, unknown> | null | undefined,
): number | null {
  return optionalPositionConstraintNumberFromSettings(settings, "max_task_position_amount");
}

export function maxTaskPositionRatioFromSettings(
  settings: Record<string, unknown> | null | undefined,
): number | null {
  return optionalPositionConstraintNumberFromSettings(settings, "max_task_position_ratio");
}

/** Read an integer ``position_constraints`` knob, preferring the nested
 * ``agent.position_constraints`` block (canonical storage) and falling back to
 * a top-level ``position_constraints`` for hand-edited settings JSON. */
function lotIntConstraintFromSettings(
  settings: Record<string, unknown> | null | undefined,
  key: "lot_size" | "rebalance_hysteresis_lots",
  fallback: number,
  minimum: number,
): number {
  const blocks = [settings?.agent?.position_constraints, settings?.position_constraints];
  for (const pc of blocks) {
    if (pc && typeof pc === "object" && !Array.isArray(pc)) {
      const raw = (pc as Record<string, unknown>)[key];
      if (typeof raw === "number" && Number.isInteger(raw) && raw >= minimum) {
        return raw;
      }
      if (typeof raw === "string" && raw.trim()) {
        const n = Number.parseInt(raw, 10);
        if (Number.isInteger(n) && n >= minimum) {
          return n;
        }
      }
    }
  }
  return fallback;
}

export function lotSizeFromSettings(settings: Record<string, unknown> | null | undefined): number {
  return lotIntConstraintFromSettings(settings, "lot_size", DEFAULT_SIGNAL_FORM.lot_size, 1);
}

export function rebalanceHysteresisFromSettings(
  settings: Record<string, unknown> | null | undefined,
): number {
  return lotIntConstraintFromSettings(
    settings,
    "rebalance_hysteresis_lots",
    DEFAULT_SIGNAL_FORM.rebalance_hysteresis_lots,
    0,
  );
}

export function minNotionalForApprovalFromSettings(settings: Record<string, unknown> | null | undefined): number {
  const approval = settings?.agent?.approval;
  if (approval && typeof approval === "object" && !Array.isArray(approval)) {
    const raw = (approval as Record<string, unknown>).min_notional_for_approval;
    if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
      return raw;
    }
    if (typeof raw === "string" && raw.trim()) {
      const n = Number.parseFloat(raw);
      if (Number.isFinite(n) && n >= 0) {
        return n;
      }
    }
  }
  return 1000; // DEFAULT_MIN_NOTIONAL_FOR_APPROVAL
}

/** Flat form fields mirroring the nested ``settings.data_cache`` block. Every
 * field is optional and ``undefined`` when the stored settings omit it, so the
 * edit form shows "unset" (and submits nothing) rather than fabricating a
 * default that would override the backend's "omitted = defaults" behavior. */
export type DataCacheFormValues = {
  data_cache_source_priority?: DataCacheSource[];
  data_cache_local_first?: boolean;
  data_cache_auto_backfill?: boolean;
  data_cache_on_unverifiable_gap?: "fail" | "degrade";
};

const DATA_CACHE_SOURCE_IDS: DataCacheSource[] = ["qmt", "baostock", "akshare", "tushare", "mock"];

/** Read ``settings.data_cache`` into the flat form fields. Unknown / malformed
 * values are dropped (left ``undefined``) rather than coerced, so a hand-edited
 * settings JSON with a bad enum surfaces as "unset" instead of silently
 * masquerading as a valid choice. */
export function dataCacheFormValuesFromSettings(
  settings: Record<string, unknown> | null | undefined,
): DataCacheFormValues {
  const raw = settings?.data_cache;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {};
  }
  const dc = raw as Record<string, unknown>;
  const out: DataCacheFormValues = {};

  if (Array.isArray(dc.source_priority)) {
    const sources = dc.source_priority.filter(
      (x): x is DataCacheSource => typeof x === "string" && DATA_CACHE_SOURCE_IDS.includes(x as DataCacheSource),
    );
    if (sources.length > 0) {
      out.data_cache_source_priority = sources;
    }
  }
  if (typeof dc.local_first === "boolean") {
    out.data_cache_local_first = dc.local_first;
  }
  if (typeof dc.auto_backfill === "boolean") {
    out.data_cache_auto_backfill = dc.auto_backfill;
  }
  const continuity = dc.continuity;
  if (continuity && typeof continuity === "object" && !Array.isArray(continuity)) {
    const cont = continuity as Record<string, unknown>;
    if (cont.on_unverifiable_gap === "fail" || cont.on_unverifiable_gap === "degrade") {
      out.data_cache_on_unverifiable_gap = cont.on_unverifiable_gap;
    }
  }
  return out;
}

export function approvalTimeoutSecondsFromSettings(settings: Record<string, unknown> | null | undefined): number {
  const approval = settings?.agent?.approval;
  if (approval && typeof approval === "object" && !Array.isArray(approval)) {
    const raw = (approval as Record<string, unknown>).timeout_seconds;
    if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
      return raw;
    }
    if (typeof raw === "string" && raw.trim()) {
      const n = Number.parseFloat(raw);
      if (Number.isFinite(n) && n > 0) {
        return n;
      }
    }
  }
  return 300; // DEFAULT_APPROVAL_TIMEOUT_SECONDS
}
