import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { beginAuthRedirect } from "./api";
import {
  ConsoleApiError,
  createConsoleKey,
  fetchConsoleUsage,
  listConsoleKeys,
  revokeConsoleKey,
} from "./consoleApi";

vi.mock("./api", () => ({
  beginAuthRedirect: vi.fn(),
}));

const fetchMock = vi.fn();

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("consoleApi", () => {
  it("createConsoleKey posts JSON with the console CSRF header and credentials", async () => {
    const result = {
      key: { id: "k1", key_prefix: "dytc_abc", name: "dev", status: "active" },
      full_key: "dytc_abc_full",
    };
    fetchMock.mockResolvedValue(jsonResponse(200, result));

    const res = await createConsoleKey("dev");

    expect(res).toEqual(result);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/console/v1/keys",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: {
          "X-Requested-With": "console",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name: "dev" }),
      }),
    );
  });

  it("revokeConsoleKey sends the CSRF header and resolves undefined on 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(revokeConsoleKey("key id/1")).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/console/v1/keys/key%20id%2F1",
      expect.objectContaining({
        method: "DELETE",
        credentials: "include",
        headers: { "X-Requested-With": "console" },
      }),
    );
  });

  it("listConsoleKeys is a plain GET with credentials (no CSRF header)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { keys: [] }));

    await expect(listConsoleKeys()).resolves.toEqual({ keys: [] });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("include");
    expect(init.method).toBeUndefined();
    expect(init.headers).toBeUndefined();
  });

  it("triggers beginAuthRedirect on 401 not_authenticated and still throws", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(401, { error_code: "not_authenticated", message: "未登录" }),
    );

    const err = await fetchConsoleUsage().catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ConsoleApiError);
    expect((err as ConsoleApiError).status).toBe(401);
    expect((err as ConsoleApiError).errorCode).toBe("not_authenticated");
    expect(beginAuthRedirect).toHaveBeenCalledTimes(1);
  });

  it("does not redirect on a 401 with a different error_code", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(401, { error_code: "token_expired", message: "token 过期" }),
    );

    await expect(fetchConsoleUsage()).rejects.toMatchObject({
      status: 401,
      errorCode: "token_expired",
    });
    expect(beginAuthRedirect).not.toHaveBeenCalled();
  });

  it("normalizes a non-JSON error response into ConsoleApiError", async () => {
    fetchMock.mockResolvedValue(new Response("<html>not found</html>", { status: 404 }));

    await expect(listConsoleKeys()).rejects.toMatchObject({
      status: 404,
      errorCode: "unknown_error",
      message: "请求失败（HTTP 404）",
    });
    expect(beginAuthRedirect).not.toHaveBeenCalled();
  });

  it("normalizes a network failure into a status-0 ConsoleApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(listConsoleKeys()).rejects.toMatchObject({
      status: 0,
      errorCode: "network_error",
    });
  });
});
