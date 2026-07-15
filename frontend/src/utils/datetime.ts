import dayjs from "dayjs";
import timezone from "dayjs/plugin/timezone";
import utc from "dayjs/plugin/utc";

dayjs.extend(utc);
dayjs.extend(timezone);

/** Display and parse helpers: API / DB timestamps are UTC (often naive ISO without offset). */

export const DISPLAY_TIMEZONE = "Asia/Shanghai";

const HAS_EXPLICIT_OFFSET = /(Z|[+-]\d{2}:\d{2}|[+-]\d{4})$/i;

/**
 * Parse a backend datetime string as an absolute instant.
 * Naive ISO strings (no Z / offset) are treated as UTC.
 */
export function parseBackendDateTime(raw: string): Date {
  const s = raw.trim();
  if (!s) return new Date(NaN);
  const normalized = s.includes("T") ? s : s.replace(" ", "T");
  if (HAS_EXPLICIT_OFFSET.test(normalized)) {
    return new Date(normalized);
  }
  return new Date(`${normalized}Z`);
}

/** Format instant in UTC+8 (China standard time) for UI tables and labels. */
export function formatDateTimeUtc8(raw: string | null | undefined, fallback = "—"): string {
  if (raw == null || raw === "") return fallback;
  const d = parseBackendDateTime(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: DISPLAY_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

/** Format an instant as a calendar date (YYYY-MM-DD) in UTC+8. Used for
 * backtest-window bounds, which are day-granular and read cleaner without the
 * noisy ``HH:mm:ss`` tail that ``formatDateTimeUtc8`` carries. */
export function formatDateUtc8(raw: string | null | undefined, fallback = "—"): string {
  if (raw == null || raw === "") return fallback;
  const d = parseBackendDateTime(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: DISPLAY_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** Render a backtest window as ``start ~ end`` at date granularity (UTC+8).
 * Returns ``fallback`` only when both bounds are missing; a single present
 * bound still renders with the other side as ``fallback``. */
export function formatBacktestRange(
  start: string | null | undefined,
  end: string | null | undefined,
  fallback = "—",
): string {
  const hasStart = start != null && start !== "";
  const hasEnd = end != null && end !== "";
  if (!hasStart && !hasEnd) return fallback;
  return `${formatDateUtc8(start, fallback)} ~ ${formatDateUtc8(end, fallback)}`;
}

export const CYCLE_PICKER_FORMAT = "YYYY-MM-DD HH:mm";

/**
 * Wall date/time chosen in the debug UI is interpreted as Asia/Shanghai (UTC+8).
 * Returns canonical UTC instant as `...Z` for `input_overrides.cycle_time`.
 */
export function cycleTimePickerToApiIso(value: dayjs.Dayjs | null | undefined): string | undefined {
  if (value == null || !value.isValid()) return undefined;
  const wall = value.format("YYYY-MM-DDTHH:mm:ss");
  const zoned = dayjs.tz(wall, DISPLAY_TIMEZONE);
  return zoned.utc().format("YYYY-MM-DDTHH:mm:ss[Z]");
}

/** Parse API `cycle_time` into a dayjs in Asia/Shanghai for DatePicker display. */
export function cycleTimeApiIsoToUtc8PickerValue(raw: string | null | undefined): dayjs.Dayjs | null {
  if (raw == null || raw.trim() === "") return null;
  const normalized = raw.trim().includes("T") ? raw.trim() : raw.trim().replace(" ", "T");
  let d = dayjs(normalized);
  if (!d.isValid()) return null;
  if (!HAS_EXPLICIT_OFFSET.test(normalized)) {
    d = dayjs.utc(normalized);
  }
  return d.tz(DISPLAY_TIMEZONE);
}
