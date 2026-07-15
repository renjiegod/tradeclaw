import { describe, expect, it } from "vitest";

import { menuKeyFromPathname } from "./menuKeyFromPathname";

describe("menuKeyFromPathname", () => {
  it("maps / to assistant (default landing)", () => {
    expect(menuKeyFromPathname("/")).toBe("assistant");
  });

  it("maps /assistant to assistant", () => {
    expect(menuKeyFromPathname("/assistant")).toBe("assistant");
  });

  it("maps /tasks and detail paths to tasks", () => {
    expect(menuKeyFromPathname("/tasks")).toBe("tasks");
    expect(menuKeyFromPathname("/tasks/abc-123")).toBe("tasks");
  });

  it("maps /channels to channels", () => {
    expect(menuKeyFromPathname("/channels")).toBe("channels");
  });

  it("maps other top-level routes", () => {
    expect(menuKeyFromPathname("/approvals")).toBe("approvals");
    expect(menuKeyFromPathname("/model_invocations")).toBe("model_invocations");
    expect(menuKeyFromPathname("/settings/models")).toBe("settings_models");
    expect(menuKeyFromPathname("/market_review")).toBe("market_review");
  });
});
