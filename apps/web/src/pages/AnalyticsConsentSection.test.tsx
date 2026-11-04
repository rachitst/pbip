import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnalyticsConsentOut } from "../api/client";
import { clickstream } from "../telemetry/tracker";
import { json, urlOf } from "../test-utils";
import { AnalyticsConsentSection } from "./AnalyticsConsentSection";

const never: AnalyticsConsentOut = {
  granted: false,
  updated_at: null,
  policy_version: null,
  current_policy_version: "ADR-0057-v1",
};

function renderSection() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AnalyticsConsentSection />
    </QueryClientProvider>,
  );
}

describe("usage analytics consent (ADR-0057)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let current: AnalyticsConsentOut = never;
  let failGet = false;
  let failPut = false;
  const puts: boolean[] = [];

  beforeEach(() => {
    current = never;
    failGet = false;
    failPut = false;
    puts.length = 0;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      if (!urlOf(input).endsWith("/customers/me/analytics-consent")) {
        return Promise.resolve(json(404, { code: "not_found" }));
      }
      if (init?.method === "PUT") {
        if (failPut) return Promise.resolve(json(500, { code: "error", title: "boom" }));
        const { granted } = JSON.parse(init.body as string) as { granted: boolean };
        puts.push(granted);
        current = {
          ...current,
          granted,
          updated_at: "2026-11-04T10:00:00Z",
          policy_version: "ADR-0057-v1",
        };
        return Promise.resolve(json(200, current));
      }
      if (failGet) return Promise.resolve(json(500, { code: "error", title: "boom" }));
      return Promise.resolve(json(200, current));
    });
  });
  afterEach(() => {
    clickstream.stop();
    vi.unstubAllGlobals();
  });

  it("is off by default and turning it on records the decision and lets the tracker send", async () => {
    const user = userEvent.setup();
    const setConsent = vi.spyOn(clickstream, "setConsent");
    renderSection();
    expect(await screen.findByText("Off")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Turn on usage analytics" }));
    expect(await screen.findByText(/^On since/)).toBeInTheDocument();
    expect(puts).toEqual([true]);
    expect(setConsent).toHaveBeenLastCalledWith(true);
    expect(screen.getByText("Policy version ADR-0057-v1")).toBeInTheDocument();
  });

  it("asks before turning it off, says the data is erased, and can be cancelled", async () => {
    const user = userEvent.setup();
    current = {
      ...never,
      granted: true,
      updated_at: "2026-11-01T10:00:00Z",
      policy_version: "ADR-0057-v1",
    };
    const setConsent = vi.spyOn(clickstream, "setConsent");
    renderSection();
    await user.click(await screen.findByRole("button", { name: "Turn off and erase" }));
    let dialog = screen.getByRole("dialog", { name: "Turn off usage analytics?" });
    expect(within(dialog).getByText(/erase the activity and profile we hold/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(puts).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Turn off and erase" }));
    dialog = screen.getByRole("dialog", { name: "Turn off usage analytics?" });
    await user.click(within(dialog).getByRole("button", { name: "Turn off and erase" }));
    expect(await screen.findByText(/^Off since/)).toBeInTheDocument();
    expect(puts).toEqual([false]);
    expect(setConsent).toHaveBeenLastCalledWith(false);
  });

  it("shows a loading error with a retry, and a failed change", async () => {
    const user = userEvent.setup();
    failGet = true;
    renderSection();
    expect(await screen.findByText("Could not load your analytics setting.")).toBeInTheDocument();
    failGet = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    failPut = true;
    await user.click(await screen.findByRole("button", { name: "Turn on usage analytics" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();
  });
});
