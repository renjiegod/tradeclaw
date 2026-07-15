export type CronParts =
  | { type: "hourly"; minute: number }
  | { type: "daily"; hour: number; minute: number }
  | { type: "weekly"; daysOfWeek: number[]; hour: number; minute: number }
  | { type: "step-minute"; everyMinutes: number }
  | { type: "step-hour"; everyHours: number; minute: number }
  | { type: "once"; month: number; day: number; hour: number; minute: number }
  | { type: "custom"; rawCron: string };

/** Form modal preset values. New schedule kinds the modal doesn't natively
 *  edit (step-minute / step-hour / once) round-trip through "custom". */
export type FormPreset = "hourly" | "daily" | "weekly" | "custom";

const INT = /^\d+$/;
const STEP = /^\*\/(\d+)$/;

function asInt(s: string): number | null {
  return INT.test(s) ? Number(s) : null;
}

function parseDayOfWeekList(s: string): number[] | null {
  const out: number[] = [];
  for (const part of s.split(",")) {
    if (!part) return null;
    if (part.includes("-")) {
      const [a, b] = part.split("-");
      const lo = asInt(a);
      const hi = asInt(b);
      if (lo === null || hi === null || lo > hi || lo < 0 || hi > 6) return null;
      for (let i = lo; i <= hi; i++) out.push(i);
    } else {
      const n = asInt(part);
      if (n === null || n < 0 || n > 6) return null;
      out.push(n);
    }
  }
  return out;
}

export function parseCron(cron: string): CronParts {
  const fields = cron.trim().split(/\s+/);
  if (fields.length !== 5) return { type: "custom", rawCron: cron };
  const [minuteF, hourF, dayF, monthF, dowF] = fields;

  // ── one-shot from the assistant tool's kind=once_at:
  //    cron expression is "M H D Mo *" with all four head fields specific.
  if (dowF === "*") {
    const minute = asInt(minuteF);
    const hour = asInt(hourF);
    const day = asInt(dayF);
    const month = asInt(monthF);
    if (
      minute !== null && hour !== null && day !== null && month !== null
      && month >= 1 && month <= 12 && day >= 1 && day <= 31
      && hour <= 23 && minute <= 59
    ) {
      return { type: "once", month, day, hour, minute };
    }
  }

  // The remaining cases all require day=* and month=*.
  if (dayF === "*" && monthF === "*") {
    // ── weekly (kind=cron, e.g. "0 9 * * 1-5")
    if (dowF !== "*") {
      const minute = asInt(minuteF);
      const hour = asInt(hourF);
      const dows = parseDayOfWeekList(dowF);
      if (minute !== null && hour !== null && dows && dows.length > 0) {
        return { type: "weekly", daysOfWeek: dows, hour, minute };
      }
      return { type: "custom", rawCron: cron };
    }

    // dow === "*" from here on.

    // ── step-minute (kind=every with sub-hour interval, "*/N * * * *")
    const minuteStep = STEP.exec(minuteF);
    if (minuteStep && hourF === "*") {
      const n = Number(minuteStep[1]);
      if (n >= 1 && n <= 59) {
        return { type: "step-minute", everyMinutes: n };
      }
    }
    if (minuteF === "*" && hourF === "*") {
      // "* * * * *" — every minute. cron tool emits this for every_seconds=60.
      return { type: "step-minute", everyMinutes: 1 };
    }

    // ── step-hour (kind=every with hour-aligned interval, "0 */N * * *")
    const hourStep = STEP.exec(hourF);
    if (hourStep) {
      const minute = asInt(minuteF);
      const n = Number(hourStep[1]);
      if (minute !== null && n >= 1 && n <= 23) {
        return { type: "step-hour", everyHours: n, minute };
      }
    }

    // ── daily ("M H * * *" with both specific)
    const dailyMin = asInt(minuteF);
    const dailyHour = asInt(hourF);
    if (dailyMin !== null && dailyHour !== null) {
      return { type: "daily", hour: dailyHour, minute: dailyMin };
    }

    // ── hourly ("M * * * *" with minute specific)
    if (hourF === "*" && dailyMin !== null) {
      return { type: "hourly", minute: dailyMin };
    }
  }

  return { type: "custom", rawCron: cron };
}

export function serializeCron(parts: CronParts): string {
  switch (parts.type) {
    case "hourly":
      return `${String(parts.minute).padStart(2, "0")} * * * *`;
    case "daily":
      return `${String(parts.minute).padStart(2, "0")} ${String(parts.hour).padStart(2, "0")} * * *`;
    case "weekly":
      return `${String(parts.minute).padStart(2, "0")} ${String(parts.hour).padStart(2, "0")} * * ${parts.daysOfWeek.join(",")}`;
    case "step-minute":
      return parts.everyMinutes === 1 ? "* * * * *" : `*/${parts.everyMinutes} * * * *`;
    case "step-hour":
      return `${parts.minute} */${parts.everyHours} * * *`;
    case "once":
      return `${parts.minute} ${parts.hour} ${parts.day} ${parts.month} *`;
    case "custom":
      return parts.rawCron;
  }
}

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function describeDaysOfWeek(days: number[]): string {
  if (days.length === 0) return "?";
  const sorted = [...new Set(days)].sort((a, b) => a - b);
  // Collapse 3+ contiguous days into Mon–Fri-style range.
  const contiguous = sorted.length >= 3
    && sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1);
  if (contiguous) {
    return `${DAY_NAMES[sorted[0]]}–${DAY_NAMES[sorted[sorted.length - 1]]}`;
  }
  return sorted.map(d => DAY_NAMES[d] ?? `?${d}`).join(",");
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

export function describeCron(cron: string): string {
  const parts = parseCron(cron);
  switch (parts.type) {
    case "hourly":
      return `Hourly at minute ${parts.minute}`;
    case "daily":
      return `Daily at ${pad2(parts.hour)}:${pad2(parts.minute)}`;
    case "weekly":
      return `Weekly on ${describeDaysOfWeek(parts.daysOfWeek)} at ${pad2(parts.hour)}:${pad2(parts.minute)}`;
    case "step-minute":
      return parts.everyMinutes === 1 ? "Every minute" : `Every ${parts.everyMinutes} minutes`;
    case "step-hour": {
      const base = parts.everyHours === 1 ? "Every hour" : `Every ${parts.everyHours} hours`;
      return parts.minute === 0 ? base : `${base} at :${pad2(parts.minute)}`;
    }
    case "once": {
      const month = MONTH_NAMES[parts.month - 1] ?? `M${parts.month}`;
      return `On ${month} ${parts.day} at ${pad2(parts.hour)}:${pad2(parts.minute)}`;
    }
    case "custom":
      return `Custom: ${cron}`;
  }
}

export function toFormPreset(type: CronParts["type"]): FormPreset {
  if (type === "hourly" || type === "daily" || type === "weekly") return type;
  return "custom";
}
