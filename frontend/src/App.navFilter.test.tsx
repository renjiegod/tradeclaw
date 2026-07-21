import { describe, expect, it } from "vitest";
import { CLOUD_HIDDEN_PAGES, visibleNavTree } from "./App";

function leafKeys(mode: string): Set<string> {
  const out = new Set<string>();
  for (const entry of visibleNavTree(mode)) {
    if ("children" in entry) entry.children.forEach((l) => out.add(l.key));
    else out.add(entry.key);
  }
  return out;
}

describe("cloud nav filtering", () => {
  it("hides local-only infra pages in cloud mode", () => {
    const keys = leafKeys("cloud");
    expect(keys.has("accounts")).toBe(false);
    expect(keys.has("settings_models")).toBe(false);
    expect(keys.has("settings")).toBe(false);
    // core copilot surface stays
    expect(keys.has("assistant")).toBe(true);
    expect(keys.has("tasks")).toBe(true);
    expect(keys.has("strategies")).toBe(true);
    expect(keys.has("model_invocations")).toBe(true);
  });

  it("shows everything in local mode (single-machine build unchanged)", () => {
    const keys = leafKeys("local");
    expect(keys.has("accounts")).toBe(true);
    expect(keys.has("settings_models")).toBe(true);
    expect(keys.has("settings")).toBe(true);
  });

  it("CLOUD_HIDDEN_PAGES is exactly the three local-only pages", () => {
    expect([...CLOUD_HIDDEN_PAGES].sort()).toEqual(["accounts", "settings", "settings_models"]);
  });

  it("keeps groups that still have visible members in cloud", () => {
    const groupKeys = visibleNavTree("cloud")
      .filter((e) => "children" in e)
      .map((e) => e.key);
    // 交易 (tasks/strategies remain) and 系统 (model_invocations remains) survive
    expect(groupKeys).toContain("grp_trading");
    expect(groupKeys).toContain("grp_system");
  });
});
