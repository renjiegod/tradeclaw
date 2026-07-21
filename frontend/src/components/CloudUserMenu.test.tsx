import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CloudUserMenu } from "./CloudUserMenu";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CloudUserMenu", () => {
  it("renders nothing in local mode (single-machine build)", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { container } = render(<CloudUserMenu mode="local" />);
    expect(container).toBeEmptyDOMElement();
    // must not even call the console API in local mode
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders nothing when mode is undefined", () => {
    const { container } = render(<CloudUserMenu mode={undefined} />);
    expect(container).toBeEmptyDOMElement();
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
    render(<CloudUserMenu mode="cloud" />);
    expect(screen.getByTestId("cloud-user-menu")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("renjiegod")).toBeInTheDocument());
  });
});
