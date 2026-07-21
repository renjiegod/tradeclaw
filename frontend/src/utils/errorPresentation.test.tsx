// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 全局错误弹窗在"会话失效正在回登录入口"期间必须静默——否则退出登录时后台在途请求
 * 连环 401 会在导航离开前一闪弹出「请求失败」。每个用例 `vi.resetModules()` 重置
 * 模块级标志,并 spy antd `Modal.error` 判断是否弹窗。
 */
describe("showErrorDialog 抑制", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal("location", { assign: vi.fn(), href: "" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("回登录跳转期间不弹「请求失败」", async () => {
    const antd = await import("antd");
    const modalSpy = vi.spyOn(antd.Modal, "error").mockReturnValue({} as ReturnType<typeof antd.Modal.error>);
    const api = await import("../api");
    const { showErrorDialog } = await import("./errorPresentation");

    api.beginAuthRedirect(false); // 置位"正在回登录入口"
    showErrorDialog(new api.ApiError('{"error":"not_authenticated"}', 401));

    expect(modalSpy).not.toHaveBeenCalled();
  });

  it("正常情况下照常弹窗", async () => {
    const antd = await import("antd");
    const modalSpy = vi.spyOn(antd.Modal, "error").mockReturnValue({} as ReturnType<typeof antd.Modal.error>);
    const api = await import("../api");
    const { showErrorDialog } = await import("./errorPresentation");

    showErrorDialog(new api.ApiError("boom", 500));

    expect(modalSpy).toHaveBeenCalled();
  });
});
