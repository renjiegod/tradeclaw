import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { SettingsPage } from "./SettingsPage";
import {
  ApiError,
  getConfig,
  getQmtProxyConfig,
  getUpdateStatus,
  updateConfig,
  updateQmtProxyConfig,
} from "../api";
import type { QmtProxyConfigResponse, DoyoutradeConfigResponse, UpdateStatus } from "../types";

// Keep the real ApiError class (SettingsPage does `instanceof ApiError`) while
// stubbing the config + update functions.
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ApiError: actual.ApiError,
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
    getQmtProxyConfig: vi.fn(),
    updateQmtProxyConfig: vi.fn(),
    getUpdateStatus: vi.fn(),
    checkForUpdate: vi.fn(),
    applyUpdate: vi.fn(),
  };
});

vi.mock("../pageRefreshContext", () => ({
  usePageRefreshToken: () => 0,
}));

const baseConfig: DoyoutradeConfigResponse = {
  path: "/home/x/.doyoutrade/config.yaml",
  values: {
    server: { host: "0.0.0.0", port: 8000, tick_seconds: 5.0 },
    data: {
      default_provider: "auto",
      tushare: { token: "********", token_set: true, url: "", timeout_seconds: 10.0 },
    },
    market_data: {
      database_url: "sqlite:///md.db",
      lookback_years: 10,
      default_provider: "auto",
      sync_on_startup: true,
      sync_concurrency: 4,
      provider_rate_limit_per_second: 2.0,
      sync_full_market: false,
    },
    observability: {
      service_name: "doyoutrade",
      log_level: "INFO",
      console_enabled: true,
      tracing_enabled: true,
    },
    review: { symbol_scope_mode: "default" },
    retention: {
      enabled: true,
      observability_ttl_days: 7,
      prune_interval_hours: 24,
      prune_on_startup: true,
    },
    assistant: {
      tool_result_max_chars: 50000,
      approval_allowlist: { rule_keys: [], command_prefixes: [] },
    },
    auto_update: { enabled: true, check_interval_hours: 6.0, repo: "renjiegod/doyoutrade" },
    database: { url: "sqlite:///main.db", echo: false, pool_pre_ping: true },
    qmt_proxy: {
      host: "127.0.0.1",
      port: 8001,
      mode: "dev",
      grpc_enabled: false,
      local_token: "********",
      local_token_set: true,
    },
    feishu: {
      enabled: false,
      app_id: "",
      app_secret: "********",
      app_secret_set: false,
      encrypt_key: "********",
      encrypt_key_set: false,
      verification_token: "********",
      verification_token_set: false,
      domain: "feishu",
    },
  },
  restart_required_fields: [
    "server.host",
    "server.port",
    "server.tick_seconds",
    "database.url",
    "market_data.database_url",
    "observability.log_level",
    "qmt_proxy.mode",
    "feishu.enabled",
  ],
};

const baseUpdateStatus: UpdateStatus = {
  enabled: true,
  check_interval_hours: 6.0,
  repo: "renjiegod/doyoutrade",
  current_version: "0.1.0",
  install_kind: "package",
  state: "idle",
  update_available: true,
  latest: {
    version: "0.2.0",
    tag: "v0.2.0",
    name: "v0.2.0",
    published_at: "2026-07-01T00:00:00Z",
    html_url: "https://github.com/renjiegod/doyoutrade/releases/tag/v0.2.0",
    notes: null,
  },
  last_checked_at: "2026-07-14T01:00:00Z",
  last_error: null,
  restart_supported: true,
};

const baseQmt: QmtProxyConfigResponse = {
  path: "/home/x/.doyoutrade/qmt-proxy.yml",
  app_mode: "dev",
  values: {
    xtquant: {
      mode: "dev",
      data: { qmt_userdata_path: null },
      trading: { allow_real_trading: false },
      clients: [],
      default_client_id: null,
      data_source_client_id: null,
    },
    security: { api_keys: ["********"], api_keys_set: true, api_keys_count: 1 },
    logging: { level: "INFO" },
    grpc: { enabled: true, host: "0.0.0.0", port: 50051 },
    app: { host: "0.0.0.0", port: 8000 },
  },
  resolved_clients: [],
  restart_required_fields: ["xtquant.mode", "security.api_keys", "app.port"],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <SettingsPage />
    </MemoryRouter>,
  );
}

// The settings form now renders inside grouped Collapse panels with
// forceRender (all ~114 Form.Items stay mounted). That makes interaction-heavy
// cases in jsdom take ~5-6s — right at vitest's 5s default. The flows
// themselves pass (verified with a raised run-level timeout; assertions are
// unchanged), so we only widen the per-test timeout, not the behavior.
const HEAVY_RENDER_TIMEOUT_MS = 15_000;

describe("SettingsPage", () => {
  beforeAll(() => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    class RO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (window as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getConfig).mockResolvedValue(structuredClone(baseConfig));
    vi.mocked(getQmtProxyConfig).mockResolvedValue(structuredClone(baseQmt));
    vi.mocked(updateConfig).mockResolvedValue({
      status: "updated",
      restart_required: true,
      restart_fields: ["server.host"],
      path: baseConfig.path,
    });
    vi.mocked(updateQmtProxyConfig).mockResolvedValue({
      status: "updated",
      restart_required: true,
      restart_fields: ["xtquant.mode"],
      path: baseQmt.path,
    });
    vi.mocked(getUpdateStatus).mockResolvedValue(structuredClone(baseUpdateStatus));
  });

  afterEach(() => {
    cleanup();
  });

  it("loads doyoutrade config and shows restart badges", async () => {
    renderPage();
    await waitFor(() => {
      expect(getConfig).toHaveBeenCalled();
    });
    const host = (await screen.findByTestId("cfg-server-host")) as HTMLInputElement;
    expect(host.value).toBe("0.0.0.0");
    // restart_required_fields carries several entries → badges rendered.
    expect(screen.getAllByText("需重启").length).toBeGreaterThan(0);
  });

  it("saves only the changed field and surfaces the restart banner", async () => {
    renderPage();
    const host = (await screen.findByTestId("cfg-server-host")) as HTMLInputElement;
    fireEvent.change(host, { target: { value: "1.2.3.4" } });

    fireEvent.click(screen.getByRole("button", { name: /保存系统配置/ }));

    await waitFor(() => {
      expect(updateConfig).toHaveBeenCalledWith({ server: { host: "1.2.3.4" } });
    });
    expect(await screen.findByText(/以下字段的改动需要重启才会生效/)).toBeInTheDocument();
  }, HEAVY_RENDER_TIMEOUT_MS);

  it("does not resend an unchanged masked secret", async () => {
    renderPage();
    const host = (await screen.findByTestId("cfg-server-host")) as HTMLInputElement;
    // change a non-secret so a patch is produced, then confirm the tushare
    // token (masked, untouched) is absent from the payload.
    fireEvent.change(host, { target: { value: "9.9.9.9" } });
    fireEvent.click(screen.getByRole("button", { name: /保存系统配置/ }));

    await waitFor(() => {
      expect(updateConfig).toHaveBeenCalledTimes(1);
    });
    const patch = vi.mocked(updateConfig).mock.calls[0]![0] as Record<string, unknown>;
    expect(patch).toEqual({ server: { host: "9.9.9.9" } });
    expect(patch).not.toHaveProperty("data");
  }, HEAVY_RENDER_TIMEOUT_MS);

  it("renders the auto_update switch (default on) and the update section", async () => {
    renderPage();
    const enabled = (await screen.findByTestId("cfg-auto-update-enabled")) as HTMLButtonElement;
    expect(enabled.getAttribute("aria-checked")).toBe("true");

    await waitFor(() => {
      expect(getUpdateStatus).toHaveBeenCalled();
    });
    const section = await screen.findByTestId("update-section");
    expect(section.textContent).toContain("v0.1.0");
    expect(section.textContent).toContain("v0.2.0");
    expect(screen.getByRole("button", { name: /立即更新到 v0\.2\.0/ })).toBeInTheDocument();
  }, HEAVY_RENDER_TIMEOUT_MS);

  it("shows guidance when the qmt-proxy is unreachable", async () => {
    vi.mocked(getQmtProxyConfig).mockRejectedValue(
      new ApiError("需要配置默认账户的 base_url 与 token", 400, {
        errorCode: "qmt_proxy_unreachable",
      }),
    );
    renderPage();
    await waitFor(() => {
      expect(getConfig).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("tab", { name: "QMT 服务端" }));

    await waitFor(() => {
      expect(getQmtProxyConfig).toHaveBeenCalled();
    });
    expect(await screen.findByText(/无法连接 qmt-proxy/)).toBeInTheDocument();
    // Guidance line unique to the unreachable branch (mentions 内嵌 both 模式).
    expect(screen.getByText(/内嵌 both 模式/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "去账户页配置" })).toBeInTheDocument();
  }, HEAVY_RENDER_TIMEOUT_MS);

  it("loads qmt-proxy config lazily on tab activation", async () => {
    renderPage();
    await waitFor(() => {
      expect(getConfig).toHaveBeenCalled();
    });
    // Not fetched while the QMT tab is inactive.
    expect(getQmtProxyConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "QMT 服务端" }));

    await waitFor(() => {
      expect(getQmtProxyConfig).toHaveBeenCalledTimes(1);
    });
  });
});
