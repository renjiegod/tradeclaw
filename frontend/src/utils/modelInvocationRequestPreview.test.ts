import { describe, expect, it } from "vitest";

import {
  LABEL_ANTHROPIC_SHAPE,
  LABEL_OPENAI_SHAPE,
  analyzeModelInvocationRequest,
} from "./modelInvocationRequestPreview";

describe("analyzeModelInvocationRequest", () => {
  it("returns markdown for Anthropic-shaped top-level system", () => {
    const r = analyzeModelInvocationRequest({
      model: "claude-test",
      system: "You are helpful.",
      messages: [{ role: "user", content: "Hi" }],
    });
    expect(r).toEqual({
      kind: "markdown",
      protocolLabel: LABEL_ANTHROPIC_SHAPE,
      text: "You are helpful.",
    });
  });

  it("prefers top-level system string over system role in messages", () => {
    const r = analyzeModelInvocationRequest({
      system: "Top",
      messages: [{ role: "system", content: "Ignored" }, { role: "user", content: "U" }],
    });
    expect(r).toEqual({
      kind: "markdown",
      protocolLabel: LABEL_ANTHROPIC_SHAPE,
      text: "Top",
    });
  });

  it("returns none for empty string top-level system", () => {
    const r = analyzeModelInvocationRequest({
      system: "",
      messages: [{ role: "user", content: "U" }],
    });
    expect(r).toEqual({ kind: "none" });
  });

  it("returns notice when top-level system is not a string", () => {
    const r = analyzeModelInvocationRequest({
      system: [{ type: "text", text: "x" }],
      messages: [],
    });
    expect(r).toEqual({
      kind: "notice",
      protocolLabel: LABEL_ANTHROPIC_SHAPE,
      message: "系统提示词无法预览（system 非字符串），请查看下方 JSON。",
    });
  });

  it("returns markdown for OpenAI-shaped first system message", () => {
    const r = analyzeModelInvocationRequest({
      model: "gpt-test",
      messages: [
        { role: "system", content: "Sys here" },
        { role: "user", content: "U" },
      ],
    });
    expect(r).toEqual({
      kind: "markdown",
      protocolLabel: LABEL_OPENAI_SHAPE,
      text: "Sys here",
    });
  });

  it("uses only the first system message when multiple exist", () => {
    const r = analyzeModelInvocationRequest({
      messages: [
        { role: "system", content: "First" },
        { role: "system", content: "Second" },
      ],
    });
    expect(r).toEqual({
      kind: "markdown",
      protocolLabel: LABEL_OPENAI_SHAPE,
      text: "First",
    });
  });

  it("normalizes system role case", () => {
    const r = analyzeModelInvocationRequest({
      messages: [{ role: "SYSTEM", content: "S" }],
    });
    expect(r).toEqual({
      kind: "markdown",
      protocolLabel: LABEL_OPENAI_SHAPE,
      text: "S",
    });
  });

  it("returns none for empty first system content", () => {
    const r = analyzeModelInvocationRequest({
      messages: [{ role: "system", content: "" }, { role: "user", content: "U" }],
    });
    expect(r).toEqual({ kind: "none" });
  });

  it("returns notice when first system content is not a string", () => {
    const r = analyzeModelInvocationRequest({
      messages: [{ role: "system", content: [{ type: "text", text: "x" }] }],
    });
    expect(r).toEqual({
      kind: "notice",
      protocolLabel: LABEL_OPENAI_SHAPE,
      message: "系统提示词无法预览（content 非字符串），请查看下方 JSON。",
    });
  });

  it("returns none for non-object and null", () => {
    expect(analyzeModelInvocationRequest(null)).toEqual({ kind: "none" });
    expect(analyzeModelInvocationRequest(undefined)).toEqual({ kind: "none" });
    expect(analyzeModelInvocationRequest("x")).toEqual({ kind: "none" });
    expect(analyzeModelInvocationRequest([])).toEqual({ kind: "none" });
  });

  it("returns none when no system anywhere", () => {
    expect(
      analyzeModelInvocationRequest({ messages: [{ role: "user", content: "U" }] }),
    ).toEqual({ kind: "none" });
  });
});
