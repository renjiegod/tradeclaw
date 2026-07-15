import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { modelInvocationTokenSummary, renderModelInvocationTokenSummary } from "./modelInvocation";
import type { ModelInvocationRow } from "../types";

function makeInvocation(overrides: Partial<ModelInvocationRow>): ModelInvocationRow {
  return {
    id: 1,
    created_at: "2026-05-02T00:00:00Z",
    model_id: "model-1",
    provider_kind: "anthropic",
    model: "claude",
    task_id: null,
    run_id: null,
    trace_id: "trace-1",
    span_id: "span-1",
    call_kind: "chat",
    first_token_latency_ms: null,
    total_latency_ms: null,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cache_read_tokens: null,
    cache_write_tokens: null,
    ok: true,
    error_message: null,
    request: {},
    response: null,
    ...overrides,
  };
}

describe("modelInvocationTokenSummary", () => {
  it("includes cache read and write token counts", () => {
    const summary = modelInvocationTokenSummary(
      makeInvocation({
        input_tokens: 100,
        output_tokens: 20,
        total_tokens: 120,
        cache_read_tokens: 300,
        cache_write_tokens: 40,
      }),
    );

    expect(summary).toContain("in 100 / out 20");
    expect(summary).toContain("合计 120");
    expect(summary).toContain("cache read 300 / write 40");
  });

  it("renders cache token summary with separate highlight styling", () => {
    render(
      <div>
        {renderModelInvocationTokenSummary(
          makeInvocation({
            input_tokens: 100,
            output_tokens: 20,
            total_tokens: 120,
            cache_read_tokens: 300,
            cache_write_tokens: 40,
          }),
        )}
      </div>,
    );

    expect(screen.getByText("in 100 / out 20 · 合计 120")).toBeInTheDocument();
    expect(screen.getByText("cache read 300 / write 40")).toHaveClass("text-orange-500");
  });
});
