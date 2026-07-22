import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { CloudUserMenu } from "./CloudUserMenu";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** 展示当前路由，供断言 SPA 内部跳转（而非 window.location 整页导航）。 */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function renderWithRouter(ui: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={["/assistant"]}>
      {ui}
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("CloudUserMenu", () => {
  it("renders nothing in local mode (single-machine build)", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderWithRouter(<CloudUserMenu mode="local" />);
    expect(screen.queryByTestId("cloud-user-menu")).not.toBeInTheDocument();
    // must not even call the console API in local mode
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders nothing when mode is undefined", () => {
    renderWithRouter(<CloudUserMenu mode={undefined} />);
    expect(screen.queryByTestId("cloud-user-menu")).not.toBeInTheDocument();
  });

  it("renders the user menu in cloud mode and shows the github login", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          user: { github_login: "renjiegod", avatar_url: null },
          tenant: { id: "tnt-1", name: "github:renjiegod" },
          quota: { daily_requests: 2000, used_today: 3, remaining_today: 1997 },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    renderWithRouter(<CloudUserMenu mode="cloud" />);
    expect(screen.getByTestId("cloud-user-menu")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("renjiegod")).toBeInTheDocument());
  });

  it("navigates to /data_console via SPA routing (no full-page navigation)", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          user: { github_login: "renjiegod", avatar_url: null },
          tenant: { id: "tnt-1", name: "github:renjiegod" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    renderWithRouter(<CloudUserMenu mode="cloud" />);

    fireEvent.click(screen.getByTestId("cloud-user-menu"));
    fireEvent.click(await screen.findByText("数据接入"));

    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/data_console");
    });
    // 内部跳转：不得触发 window.location 整页导航（jsdom 中 location 保持初始值）
    expect(window.location.pathname).toBe("/");
  });
});
