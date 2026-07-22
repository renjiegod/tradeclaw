import { describe, expect, it } from "vitest";
import { CLOUD_HIDDEN_PAGES, CLOUD_ONLY_PAGES, visibleNavTree } from "./App";

function leafKeys(mode: string | null): Set<string> {
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

  it("shows the cloud-only data console in cloud mode only", () => {
    expect(leafKeys("cloud").has("data_console")).toBe(true);
    expect(leafKeys("local").has("data_console")).toBe(false);
    // mode not yet resolved (null) behaves like local: no cloud-only entries
    expect(leafKeys(null).has("data_console")).toBe(false);
  });

  it("shows everything except cloud-only pages in local mode", () => {
    const keys = leafKeys("local");
    expect(keys.has("accounts")).toBe(true);
    expect(keys.has("settings_models")).toBe(true);
    expect(keys.has("settings")).toBe(true);
    expect(keys.has("data_console")).toBe(false);
  });

  it("CLOUD_HIDDEN_PAGES is exactly the three local-only pages", () => {
    expect([...CLOUD_HIDDEN_PAGES].sort()).toEqual(["accounts", "settings", "settings_models"]);
  });

  it("CLOUD_ONLY_PAGES is exactly the data console", () => {
    expect([...CLOUD_ONLY_PAGES].sort()).toEqual(["data_console"]);
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
