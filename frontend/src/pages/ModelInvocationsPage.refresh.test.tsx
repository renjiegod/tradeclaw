import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PageRefreshContext } from "../pageRefreshContext";
import { listModelInvocations } from "../api";
import { ModelInvocationsPage } from "./ModelInvocationsPage";

vi.mock("../api", () => ({
  listModelInvocations: vi.fn(),
}));

vi.mock("../components/PageIntro", () => ({
  PageIntro: ({ title }: { title: string }) => <div>{title}</div>,
}));

vi.mock("../components/JsonCodeBlock", () => ({
  JsonCodeBlock: () => null,
}));

vi.mock("../components/ModelInvocationRequestPanel", () => ({
  ModelInvocationRequestPanel: () => null,
}));

vi.mock("../components/TabbedJsonPanel", () => ({
  TabbedJsonPanel: () => null,
}));

vi.mock("../components/FormattedRequestView", () => ({
  FormattedRequestView: () => null,
}));

vi.mock("../components/FormattedResponseView", () => ({
  FormattedResponseView: () => null,
}));

describe("ModelInvocationsPage refresh token", () => {
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
    vi.mocked(listModelInvocations).mockResolvedValue({
      items: [],
      total: 0,
    });
  });

  it("re-fetches page data when the shell refresh token changes", async () => {
    const view = render(
      <PageRefreshContext.Provider value={0}>
        <ModelInvocationsPage />
      </PageRefreshContext.Provider>,
    );

    await waitFor(() => {
      expect(listModelInvocations).toHaveBeenCalledTimes(1);
    });

    view.rerender(
      <PageRefreshContext.Provider value={1}>
        <ModelInvocationsPage />
      </PageRefreshContext.Provider>,
    );

    await waitFor(() => {
      expect(listModelInvocations).toHaveBeenCalledTimes(2);
    });
  });
});
