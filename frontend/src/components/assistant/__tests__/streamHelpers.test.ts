// frontend/src/components/assistant/__tests__/streamHelpers.test.ts

import { describe, expect, it } from "vitest";

import { isEventForCurrentAttempt } from "../streamHelpers";

describe("isEventForCurrentAttempt", () => {
  it("accepts an event whose attempt_id matches the current attempt", () => {
    expect(isEventForCurrentAttempt({ attempt_id: "attempt-new" }, "attempt-new")).toBe(true);
  });

  it("rejects a stale/foreign event whose attempt_id belongs to a different attempt", () => {
    // Regression guard: a replayed tool.call event from a previous, already
    // finished attempt must not be attributed to the turn currently being
    // rendered — this is what let an old tool-call card flash beneath a
    // freshly-sent user message.
    expect(isEventForCurrentAttempt({ attempt_id: "attempt-old" }, "attempt-new")).toBe(false);
  });

  it("lets an event through when attempt_id is missing rather than silently dropping it", () => {
    expect(isEventForCurrentAttempt({}, "attempt-new")).toBe(true);
    expect(isEventForCurrentAttempt({ attempt_id: 123 }, "attempt-new")).toBe(true);
  });

  it("rejects when the current attempt is still unset (fresh submit, no attempt.started yet)", () => {
    expect(isEventForCurrentAttempt({ attempt_id: "attempt-old" }, "")).toBe(false);
  });
});
