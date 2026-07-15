import { describe, expect, it } from "vitest";
import { tryParseJsonString } from "./jsonSyntaxHint";

describe("tryParseJsonString", () => {
  it("returns parsed object for valid JSON string", () => {
    const result = tryParseJsonString('{"key": "value"}');
    expect(result).toEqual({ key: "value" });
  });

  it("returns null for plain text", () => {
    const result = tryParseJsonString("hello world");
    expect(result).toBeNull();
  });

  it("returns null for invalid JSON", () => {
    const result = tryParseJsonString('{"key": }');
    expect(result).toBeNull();
  });

  it("returns null for null/undefined", () => {
    expect(tryParseJsonString(null)).toBeNull();
    expect(tryParseJsonString(undefined)).toBeNull();
  });

  it("returns null for non-string input", () => {
    expect(tryParseJsonString(123)).toBeNull();
    expect(tryParseJsonString({ key: "value" })).toBeNull();
  });

  it("returns null for JSON array", () => {
    expect(tryParseJsonString("[1, 2, 3]")).toBeNull();
    expect(tryParseJsonString("[]")).toBeNull();
  });

  it("returns null for JSON primitives", () => {
    expect(tryParseJsonString("123")).toBeNull();
    expect(tryParseJsonString("true")).toBeNull();
    expect(tryParseJsonString('"hello"')).toBeNull();
  });

  it("returns parsed object for empty object", () => {
    const result = tryParseJsonString("{}");
    expect(result).toEqual({});
  });
});
