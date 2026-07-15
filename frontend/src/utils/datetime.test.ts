import dayjs from "dayjs";
import timezone from "dayjs/plugin/timezone";
import utc from "dayjs/plugin/utc";
import { describe, expect, it } from "vitest";

dayjs.extend(utc);
dayjs.extend(timezone);

import {
  cycleTimePickerToApiIso,
  cycleTimeApiIsoToUtc8PickerValue,
  formatBacktestRange,
  formatDateUtc8,
} from "./datetime";

describe("cycle_time UTC+8 picker", () => {
  it("serializes Shanghai wall time to UTC Z", () => {
    const wall = dayjs.tz("2026-04-01 16:00:00", "Asia/Shanghai");
    expect(cycleTimePickerToApiIso(wall)).toBe("2026-04-01T08:00:00Z");
  });

  it("round-trips Z through picker value", () => {
    const back = cycleTimeApiIsoToUtc8PickerValue("2026-04-01T08:00:00Z");
    expect(back?.format("YYYY-MM-DD HH:mm")).toBe("2026-04-01 16:00");
  });
});

describe("formatDateUtc8", () => {
  it("renders a naive UTC instant as the UTC+8 calendar date", () => {
    // 2026-01-05T20:00:00Z is 2026-01-06 04:00 in UTC+8.
    expect(formatDateUtc8("2026-01-05T20:00:00")).toBe("2026-01-06");
  });

  it("honors an explicit Z offset", () => {
    expect(formatDateUtc8("2026-01-05T00:00:00Z")).toBe("2026-01-05");
  });

  it("returns the fallback for empty / nullish input", () => {
    expect(formatDateUtc8(null)).toBe("—");
    expect(formatDateUtc8("")).toBe("—");
    expect(formatDateUtc8(undefined, "n/a")).toBe("n/a");
  });
});

describe("formatBacktestRange", () => {
  it("joins both bounds with a tilde", () => {
    expect(formatBacktestRange("2026-01-01T00:00:00Z", "2026-06-30T00:00:00Z")).toBe(
      "2026-01-01 ~ 2026-06-30",
    );
  });

  it("renders a single present bound with a dash placeholder for the other", () => {
    expect(formatBacktestRange("2026-01-01T00:00:00Z", null)).toBe("2026-01-01 ~ —");
  });

  it("returns the fallback when both bounds are missing", () => {
    expect(formatBacktestRange(null, null)).toBe("—");
    expect(formatBacktestRange("", "")).toBe("—");
  });
});
