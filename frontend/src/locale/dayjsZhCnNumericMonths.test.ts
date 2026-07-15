import { describe, expect, it } from "vitest";
import dayjs from "dayjs";
import localeData from "dayjs/plugin/localeData";

import "./dayjsZhCnNumericMonths";

dayjs.extend(localeData);

describe("dayjsZhCnNumericMonths", () => {
  it("uses Arabic month index + 月 for MMMM under zh-cn", () => {
    const d = dayjs("2026-04-18").locale("zh-cn");
    expect(d.format("MMMM")).toBe("4 月");
  });

  it("uses the same labels for MMM", () => {
    const d = dayjs("2026-12-01").locale("zh-cn");
    expect(d.format("MMM")).toBe("12 月");
  });
});
