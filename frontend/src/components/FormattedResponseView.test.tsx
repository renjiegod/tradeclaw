import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";

import { FormattedResponseView } from "./FormattedResponseView";

describe("FormattedResponseView", () => {
  it("can be imported", () => {
    expect(FormattedResponseView).toBeDefined();
  });

  it("renders text block without throwing for anthropic", () => {
    expect(() =>
      renderToString(
        <FormattedResponseView
          provider_kind="anthropic"
          data={{ content: [{ type: "text", text: "Hello **world**" }] }}
        />
      )
    ).not.toThrow();
  });

  it("renders tool_use block without throwing for anthropic", () => {
    expect(() =>
      renderToString(
        <FormattedResponseView
          provider_kind="anthropic"
          data={{
            content: [
              {
                type: "tool_use",
                id: "tool_1",
                name: "get_weather",
                input: { symbol: "AAPL" },
              },
            ],
          }}
        />
      )
    ).not.toThrow();
  });

  it("renders thinking block without throwing for anthropic", () => {
    expect(() =>
      renderToString(
        <FormattedResponseView
          provider_kind="anthropic"
          data={{ content: [{ type: "thinking", thinking: "Let me think about this..." }] }}
        />
      )
    ).not.toThrow();
  });

  it("renders OpenAI choices message content without throwing", () => {
    expect(() =>
      renderToString(
        <FormattedResponseView
          provider_kind="openai_compatible"
          data={{ choices: [{ message: { role: "assistant", content: "Hello **world**" } }] }}
        />
      )
    ).not.toThrow();
  });

  it("renders OpenAI tool_calls without throwing", () => {
    expect(() =>
      renderToString(
        <FormattedResponseView
          provider_kind="openai_compatible"
          data={{
            choices: [
              {
                message: {
                  role: "assistant",
                  content: "",
                  tool_calls: [
                    {
                      id: "1",
                      function: { name: "get_weather", arguments: { symbol: "AAPL" } },
                    },
                  ],
                },
              },
            ],
          }}
        />
      )
    ).not.toThrow();
  });

  it("renders fallback for empty response without throwing", () => {
    expect(() =>
      renderToString(<FormattedResponseView provider_kind="anthropic" data={{ content: [] }} />)
    ).not.toThrow();
  });
});
