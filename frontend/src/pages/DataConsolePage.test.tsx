import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DataConsolePage } from "./DataConsolePage";
import {
  ConsoleApiError,
  createConsoleKey,
  fetchConsoleMe,
  fetchConsoleUsage,
  listConsoleKeys,
  revokeConsoleKey,
} from "../consoleApi";

vi.mock("../consoleApi", async () => {
  const actual = await vi.importActual<typeof import("../consoleApi")>("../consoleApi");
  return {
    ...actual,
    createConsoleKey: vi.fn(),
    fetchConsoleMe: vi.fn(),
    fetchConsoleUsage: vi.fn(),
    listConsoleKeys: vi.fn(),
    revokeConsoleKey: vi.fn(),
  };
});

const activeKey = {
  id: "key-1",
  key_prefix: "dytc_abc123",
  name: "本地开发",
  status: "active" as const,
  created_at: "2026-07-01T10:00:00Z",
  expires_at: null,
  last_used_at: "2026-07-20T09:30:00Z",
};

const revokedKey = {
  id: "key-2",
  key_prefix: "dytc_old456",
  name: null,
  status: "revoked" as const,
  created_at: "2026-06-01T10:00:00Z",
  expires_at: null,
  last_used_at: null,
};

const usage = {
  date: "2026-07-22",
  today: {
    requests: 120,
    cache_hits: 80,
    errors: 2,
    by_operation: { trading_calendar: { requests: 100, cache_hits: 70, errors: 1 } },
  },
  month: {
    requests: 3400,
    cache_hits: 2100,
    errors: 9,
    by_operation: { market_data: { requests: 3000, cache_hits: 2000, errors: 8 } },
  },
  quota: { daily_requests: 2000, used_today: 120, remaining_today: 1880 },
};

const me = {
  user: {
    id: "u1",
    github_login: "renjiegod",
    name: "renjiegod",
    avatar_url: null,
    is_admin: false,
    status: "active" as const,
    plan: {
      plan_name: "free",
      rate_per_minute: 60,
      daily_requests: 2000,
      scopes: ["market"],
      max_ws_connections: 1,
    },
  },
  quota: { daily_requests: 2000, used_today: 120, remaining_today: 1880 },
};

describe("DataConsolePage", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
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
    vi.mocked(listConsoleKeys).mockResolvedValue({ keys: [activeKey, revokedKey] });
    vi.mocked(fetchConsoleUsage).mockResolvedValue(usage);
    vi.mocked(fetchConsoleMe).mockResolvedValue(me);
  });

  it("lists API keys with status tags", async () => {
    render(<DataConsolePage />);

    expect(await screen.findByText("dytc_abc123…")).toBeInTheDocument();
    expect(screen.getByText("dytc_old456…")).toBeInTheDocument();
    expect(screen.getByText("本地开发")).toBeInTheDocument();
    expect(screen.getByText("未命名")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("revoked")).toBeInTheDocument();
    // revoked key has no 吊销 action
    expect(screen.getAllByRole("button", { name: /^吊销 dytc_/ })).toHaveLength(1);
  });

  it("creates a key and reveals the full key exactly once in a guarded modal", async () => {
    vi.mocked(createConsoleKey).mockResolvedValue({
      key: activeKey,
      full_key: "dytc_abc123_THE_FULL_SECRET",
    });
    render(<DataConsolePage />);

    fireEvent.click(await screen.findByRole("button", { name: "创建 Key" }));
    fireEvent.change(screen.getByLabelText("Key 名称"), { target: { value: " 本地开发 " } });
    fireEvent.click(screen.getByRole("button", { name: "创 建" }));

    await waitFor(() => {
      expect(createConsoleKey).toHaveBeenCalledWith("本地开发");
    });
    const reveal = await screen.findByTestId("full-key-value");
    expect(reveal).toHaveTextContent("dytc_abc123_THE_FULL_SECRET");
    expect(screen.getByText(/完整 key 只显示这一次/)).toBeInTheDocument();
    // list is reloaded after creation
    expect(listConsoleKeys).toHaveBeenCalledTimes(2);
  });

  it("sends null when the key name is left blank", async () => {
    vi.mocked(createConsoleKey).mockResolvedValue({ key: activeKey, full_key: "dytc_x" });
    render(<DataConsolePage />);

    fireEvent.click(await screen.findByRole("button", { name: "创建 Key" }));
    fireEvent.click(screen.getByRole("button", { name: "创 建" }));

    await waitFor(() => {
      expect(createConsoleKey).toHaveBeenCalledWith(null);
    });
  });

  it("shows key_limit_reached as an alert inside the create modal", async () => {
    vi.mocked(createConsoleKey).mockRejectedValue(
      new ConsoleApiError(403, "key_limit_reached", "最多只能创建 5 个 key"),
    );
    render(<DataConsolePage />);

    fireEvent.click(await screen.findByRole("button", { name: "创建 Key" }));
    fireEvent.click(screen.getByRole("button", { name: "创 建" }));

    expect(await screen.findByText("最多只能创建 5 个 key")).toBeInTheDocument();
    // modal stays open (input still present), no full-key reveal
    expect(screen.getByLabelText("Key 名称")).toBeInTheDocument();
    expect(screen.queryByTestId("full-key-value")).not.toBeInTheDocument();
  });

  it("revokes a key after Popconfirm confirmation", async () => {
    vi.mocked(revokeConsoleKey).mockResolvedValue(undefined);
    render(<DataConsolePage />);

    fireEvent.click(await screen.findByRole("button", { name: "吊销 dytc_abc123" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认吊销" }));

    await waitFor(() => {
      expect(revokeConsoleKey).toHaveBeenCalledWith("key-1");
    });
    // list reloads after revocation
    await waitFor(() => {
      expect(listConsoleKeys).toHaveBeenCalledTimes(2);
    });
  });

  it("shows a retry-able error when the key list fails to load", async () => {
    vi.mocked(listConsoleKeys)
      .mockRejectedValueOnce(new ConsoleApiError(0, "network_error", "网络错误，请检查连接后重试"))
      .mockResolvedValueOnce({ keys: [activeKey] });
    render(<DataConsolePage />);

    expect(await screen.findByText("加载 API Keys 失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重 试" }));

    expect(await screen.findByText("dytc_abc123…")).toBeInTheDocument();
  });

  it("renders quota, per-period stats and by-operation usage", async () => {
    render(<DataConsolePage />);

    fireEvent.click(screen.getByRole("tab", { name: "用量" }));

    expect(await screen.findByText(/套餐 free/)).toBeInTheDocument();
    expect(screen.getByText("今日配额")).toBeInTheDocument();
    expect(screen.getByText("剩余 1880")).toBeInTheDocument();
    expect(screen.getByText("trading_calendar")).toBeInTheDocument();

    // 切到本月：by_operation 表随 Segmented 切换
    fireEvent.click(screen.getByText("本月", { selector: ".ant-segmented-item-label" }));
    expect(await screen.findByText("market_data")).toBeInTheDocument();
  });

  it("tolerates a /me failure and still renders usage", async () => {
    vi.mocked(fetchConsoleMe).mockRejectedValue(
      new ConsoleApiError(500, "internal_error", "boom"),
    );
    render(<DataConsolePage />);

    fireEvent.click(screen.getByRole("tab", { name: "用量" }));

    expect(await screen.findByText("今日配额")).toBeInTheDocument();
    expect(screen.queryByText(/套餐 free/)).not.toBeInTheDocument();
  });

  it("shows a retry-able error when usage fails to load", async () => {
    vi.mocked(fetchConsoleUsage)
      .mockRejectedValueOnce(new ConsoleApiError(502, "bad_gateway", "网关不可用"))
      .mockResolvedValueOnce(usage);
    render(<DataConsolePage />);

    fireEvent.click(screen.getByRole("tab", { name: "用量" }));

    expect(await screen.findByText("加载用量失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重 试" }));

    expect(await screen.findByText("今日配额")).toBeInTheDocument();
  });

  it("renders the guide with the hardcoded cloud data base URL", async () => {
    render(<DataConsolePage />);

    fireEvent.click(screen.getByRole("tab", { name: "接入教程" }));

    expect(await screen.findByText("https://api.doyoutrade.cloud")).toBeInTheDocument();
    expect(
      screen.getByText(/api\.doyoutrade\.cloud\/api\/cloud\/v1\/trading-calendar/),
    ).toBeInTheDocument();
    expect(screen.getByText("创建 API Key")).toBeInTheDocument();
    expect(screen.getByText("在本地 DoYouTrade 客户端中新建账户")).toBeInTheDocument();
    expect(screen.getByText("用 curl 验证连通性")).toBeInTheDocument();
    expect(screen.getByText("注意事项")).toBeInTheDocument();
  });
});
