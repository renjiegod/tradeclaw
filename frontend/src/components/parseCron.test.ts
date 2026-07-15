import { describe, expect, it } from "vitest";

import { describeCron, parseCron, serializeCron, toFormPreset } from "./parseCron";

describe("parseCron — assistant cron tool patterns", () => {
  it("recognises kind=once_at output ('M H D Mo *')", () => {
    // create_cron_job with schedule={kind:'once_at', delay_seconds:30} at
    // 2026-05-16T22:52 produces "53 22 16 5 *".
    expect(parseCron("53 22 16 5 *")).toEqual({
      type: "once",
      month: 5,
      day: 16,
      hour: 22,
      minute: 53,
    });
  });

  it("describes kind=once_at as a one-shot, not 'Daily'", () => {
    expect(describeCron("53 22 16 5 *")).toBe("On May 16 at 22:53");
  });

  it("recognises kind=every sub-hour ('*/N * * * *')", () => {
    expect(parseCron("*/5 * * * *")).toEqual({ type: "step-minute", everyMinutes: 5 });
    expect(parseCron("*/30 * * * *")).toEqual({ type: "step-minute", everyMinutes: 30 });
    expect(describeCron("*/5 * * * *")).toBe("Every 5 minutes");
    expect(describeCron("*/30 * * * *")).toBe("Every 30 minutes");
  });

  it("recognises every_seconds=60 ('* * * * *')", () => {
    expect(parseCron("* * * * *")).toEqual({ type: "step-minute", everyMinutes: 1 });
    expect(describeCron("* * * * *")).toBe("Every minute");
  });

  it("recognises kind=every hour-aligned ('0 */N * * *')", () => {
    expect(parseCron("0 */6 * * *")).toEqual({
      type: "step-hour",
      everyHours: 6,
      minute: 0,
    });
    expect(describeCron("0 */6 * * *")).toBe("Every 6 hours");
    expect(describeCron("0 */2 * * *")).toBe("Every 2 hours");
  });

  it("recognises weekday ranges from kind=cron ('0 9 * * 1-5')", () => {
    expect(parseCron("0 9 * * 1-5")).toEqual({
      type: "weekly",
      daysOfWeek: [1, 2, 3, 4, 5],
      hour: 9,
      minute: 0,
    });
    expect(describeCron("0 9 * * 1-5")).toBe("Weekly on Mon–Fri at 09:00");
  });
});

describe("parseCron — existing form modal patterns still work", () => {
  it("keeps 'M H * * *' as daily", () => {
    expect(parseCron("0 9 * * *")).toEqual({ type: "daily", hour: 9, minute: 0 });
    expect(describeCron("0 9 * * *")).toBe("Daily at 09:00");
  });

  it("keeps 'M * * * *' as hourly", () => {
    expect(parseCron("15 * * * *")).toEqual({ type: "hourly", minute: 15 });
    expect(describeCron("15 * * * *")).toBe("Hourly at minute 15");
  });

  it("treats every_seconds=86400 ('0 0 * * *') as daily 00:00 for backward compat", () => {
    // Form modal preset round-tripping needs this to stay daily.
    expect(parseCron("0 0 * * *")).toEqual({ type: "daily", hour: 0, minute: 0 });
  });

  it("treats every_seconds=3600 ('0 * * * *') as hourly for backward compat", () => {
    expect(parseCron("0 * * * *")).toEqual({ type: "hourly", minute: 0 });
  });

  it("handles weekly with comma-separated days", () => {
    expect(parseCron("0 8 * * 1,3,5")).toEqual({
      type: "weekly",
      daysOfWeek: [1, 3, 5],
      hour: 8,
      minute: 0,
    });
    expect(describeCron("0 8 * * 1,3,5")).toBe("Weekly on Mon,Wed,Fri at 08:00");
  });

  it("handles weekly with single day", () => {
    expect(parseCron("30 7 * * 0")).toEqual({
      type: "weekly",
      daysOfWeek: [0],
      hour: 7,
      minute: 30,
    });
    expect(describeCron("30 7 * * 0")).toBe("Weekly on Sun at 07:30");
  });
});

describe("parseCron — fallback to custom", () => {
  it("falls back when fields !== 5", () => {
    expect(parseCron("0 9 * *")).toEqual({ type: "custom", rawCron: "0 9 * *" });
    expect(parseCron("0 9 * * * 2026")).toEqual({ type: "custom", rawCron: "0 9 * * * 2026" });
  });

  it("falls back on unknown forms (hour range)", () => {
    expect(parseCron("0 9-17 * * *")).toEqual({ type: "custom", rawCron: "0 9-17 * * *" });
  });

  it("falls back on weekly with out-of-range days", () => {
    expect(parseCron("0 9 * * 7")).toEqual({ type: "custom", rawCron: "0 9 * * 7" });
  });
});

describe("serializeCron round-trips", () => {
  it("re-parses to the same shape (cron strings may pad minute/hour)", () => {
    const cases = [
      "53 22 16 5 *",
      "*/5 * * * *",
      "* * * * *",
      "0 */6 * * *",
      "0 9 * * 1-5",
      "0 9 * * *",
      "15 * * * *",
      "0 0 * * *",
    ];
    for (const expr of cases) {
      const parts = parseCron(expr);
      const reparsed = parseCron(serializeCron(parts));
      expect(reparsed).toEqual(parts);
    }
  });
});

describe("toFormPreset", () => {
  it("passes through form-native presets", () => {
    expect(toFormPreset("hourly")).toBe("hourly");
    expect(toFormPreset("daily")).toBe("daily");
    expect(toFormPreset("weekly")).toBe("weekly");
    expect(toFormPreset("custom")).toBe("custom");
  });

  it("maps new schedule kinds to custom so the form can still edit them", () => {
    expect(toFormPreset("once")).toBe("custom");
    expect(toFormPreset("step-minute")).toBe("custom");
    expect(toFormPreset("step-hour")).toBe("custom");
  });
});
