import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import { FormattedRequestView } from "./FormattedRequestView";

describe("FormattedRequestView", () => {
  it("can be imported", () => {
    expect(FormattedRequestView).toBeDefined();
  });

  it("renders without throwing for anthropic with system prompt", () => {
    expect(() =>
      renderToString(
        <FormattedRequestView
          provider="anthropic"
          data={{ system: "You are a helpful assistant.", messages: [] }}
        />
      )
    ).not.toThrow();
  });

  it("renders without throwing for openai_compatible with tools", () => {
    expect(() =>
      renderToString(
        <FormattedRequestView
          provider="openai_compatible"
          data={{
            model: "gpt-4",
            messages: [],
            tools: [{ type: "function", function: { name: "get_weather", parameters: {} } }],
          }}
        />
      )
    ).not.toThrow();
  });

  it("renders fallback alert when nothing can be formatted", () => {
    expect(() =>
      renderToString(
        <FormattedRequestView provider="anthropic" data={{ model: "claude-3" }} />
      )
    ).not.toThrow();
  });

  it("renders without throwing for openai_compatible with system message in messages", () => {
    expect(() =>
      renderToString(
        <FormattedRequestView
          provider="openai_compatible"
          data={{
            model: "gpt-4",
            messages: [
              { role: "system", content: "You are a helpful assistant." },
              { role: "user", content: "hi" },
            ],
          }}
        />
      )
    ).not.toThrow();
  });

  it("renders Markdown for Anthropic-style user message with text blocks", () => {
    const html = renderToString(
      <FormattedRequestView
        provider="anthropic"
        data={{
          model: "x",
          messages: [
            {
              role: "user",
              content: [{ type: "text", text: "## Cycle\n\n- **a**: 1" }],
            },
          ],
        }}
      />
    );
    expect(html).toContain("Cycle");
    expect(html).toMatch(/<h2[^>]*>/i);
  });

  it("renders tool_use and tool_result blocks without throwing", () => {
    expect(() =>
      renderToString(
        <FormattedRequestView
          provider="anthropic"
          data={{
            messages: [
              {
                role: "assistant",
                content: [
                  {
                    type: "tool_use",
                    id: "call_1",
                    name: "data_bars_relative",
                    input: { symbol: "002506.SZ" },
                  },
                ],
              },
              {
                role: "user",
                content: [
                  {
                    type: "tool_result",
                    tool_use_id: "call_1",
                    content: '{"data_bars":{"symbol":"002506.SZ","bars":[]}}',
                  },
                ],
              },
            ],
          }}
        />
      )
    ).not.toThrow();
  });
});
