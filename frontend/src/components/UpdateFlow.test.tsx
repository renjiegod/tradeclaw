import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UpdateBanner } from "./UpdateFlow";
import { getUpdateStatus } from "../api";
import type { UpdateStatus } from "../types";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ApiError: actual.ApiError,
    getUpdateStatus: vi.fn(),
    checkForUpdate: vi.fn(),
    applyUpdate: vi.fn(),
  };
});

const status: UpdateStatus = {
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

describe("UpdateBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
  });

  it("prompts when an update is available and offers the apply button", async () => {
    vi.mocked(getUpdateStatus).mockResolvedValue(structuredClone(status));
    render(<UpdateBanner />);
    expect(await screen.findByText(/发现新版本/)).toBeInTheDocument();
    expect(screen.getByText("v0.2.0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即更新" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看发布说明" })).toHaveAttribute(
      "href",
      status.latest!.html_url!,
    );
  });

  it("stays hidden when no update is available", async () => {
    vi.mocked(getUpdateStatus).mockResolvedValue({
      ...structuredClone(status),
      update_available: false,
    });
    render(<UpdateBanner />);
    await waitFor(() => {
      expect(getUpdateStatus).toHaveBeenCalled();
    });
    expect(screen.queryByText(/发现新版本/)).not.toBeInTheDocument();
  });

  it("stays hidden when the update service is unavailable (fetch throws)", async () => {
    vi.mocked(getUpdateStatus).mockRejectedValue(new Error("503"));
    render(<UpdateBanner />);
    await waitFor(() => {
      expect(getUpdateStatus).toHaveBeenCalled();
    });
    expect(screen.queryByText(/发现新版本/)).not.toBeInTheDocument();
  });

  it("dismissal is remembered per release tag", async () => {
    vi.mocked(getUpdateStatus).mockResolvedValue(structuredClone(status));
    render(<UpdateBanner />);
    await screen.findByText(/发现新版本/);
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => {
      expect(screen.queryByText(/发现新版本/)).not.toBeInTheDocument();
    });
    expect(localStorage.getItem("update_banner_dismissed_tag")).toBe("v0.2.0");

    // A fresh mount for the SAME tag stays dismissed.
    cleanup();
    render(<UpdateBanner />);
    await waitFor(() => {
      expect(getUpdateStatus).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByText(/发现新版本/)).not.toBeInTheDocument();
  });
});
