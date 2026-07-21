// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 退出登录 / 会话失效的 401 处理。
 *
 * cloud 网关对无有效 session 的非导航请求返回 `401 {"error":"not_authenticated"}`;
 * 退出登录清 cookie 与整页导航之间的竞态会让后台请求命中它。前端应统一"回登录入口
 * + 抑制弹窗",而不是弹「请求失败」。每个用例用 `vi.resetModules()` 拿到全新的模块
 * 单例,避免模块级 `authRedirectInFlight` 标志跨用例污染。
 */
describe("未认证 401 → 回登录入口", () => {
  let assignMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    // jsdom 的 window.location.assign 不可 spy(non-configurable);整体替换 location。
    // 需带 origin —— api.ts 顶层 WS_BASE 在重新 import 时会读它。
    assignMock = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { assign: assignMock, href: "", origin: "http://localhost" },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubFetch(status: number, body: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(typeof body === "string" ? body : JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  }

  it("网关裸 401 body 触发整页跳转并置位抑制标志", async () => {
    const { listTasks, isAuthRedirectInFlight } = await import("./api");
    stubFetch(401, { error: "not_authenticated" });

    await expect(listTasks()).rejects.toMatchObject({ name: "ApiError", status: 401 });
    expect(isAuthRedirectInFlight()).toBe(true);
    expect(assignMock).toHaveBeenCalledWith("/");
  });

  it("dytc 结构化 not_authenticated(error_code)也触发跳转", async () => {
    const { listTasks, isAuthRedirectInFlight } = await import("./api");
    stubFetch(401, { detail: "会话已过期", error_code: "not_authenticated" });

    await expect(listTasks()).rejects.toMatchObject({ status: 401 });
    expect(isAuthRedirectInFlight()).toBe(true);
    expect(assignMock).toHaveBeenCalledWith("/");
  });

  it("非 not_authenticated 的 401 不跳转、不置位(照常暴露错误)", async () => {
    const { listTasks, isAuthRedirectInFlight } = await import("./api");
    stubFetch(401, { detail: "csrf token invalid", error_code: "csrf_failed" });

    await expect(listTasks()).rejects.toMatchObject({ status: 401 });
    expect(isAuthRedirectInFlight()).toBe(false);
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("非 401 错误不触发跳转", async () => {
    const { listTasks, isAuthRedirectInFlight } = await import("./api");
    stubFetch(500, { detail: "boom" });

    await expect(listTasks()).rejects.toMatchObject({ status: 500 });
    expect(isAuthRedirectInFlight()).toBe(false);
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("beginAuthRedirect(false) 只置位、不自行跳转(退出登录按钮自己跳)", async () => {
    const { beginAuthRedirect, isAuthRedirectInFlight } = await import("./api");
    beginAuthRedirect(false);

    expect(isAuthRedirectInFlight()).toBe(true);
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("beginAuthRedirect 幂等:重复调用只跳一次", async () => {
    const { beginAuthRedirect } = await import("./api");
    beginAuthRedirect();
    beginAuthRedirect();

    expect(assignMock).toHaveBeenCalledTimes(1);
  });
});
