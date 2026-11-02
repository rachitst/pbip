import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountsSummaryOut } from "../api/client";
import { TotalBalance } from "./TotalBalance";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const inr = {
  currency: "INR",
  ledger_balance_minor: 157_000_00,
  held_minor: 25_000_00,
  available_minor: 125_000_00,
  account_count: 3,
  frozen_account_count: 1,
};
const usd = {
  currency: "USD",
  ledger_balance_minor: 7_50,
  held_minor: 0,
  available_minor: 7_50,
  account_count: 1,
  frozen_account_count: 0,
};

describe("total balance overview", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function respondWith(...responses: Response[]) {
    for (const response of responses) fetchMock.mockResolvedValueOnce(response);
  }

  function renderTotal() {
    return render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <TotalBalance />
      </QueryClientProvider>,
    );
  }

  it("shows loading, then the server's totals per currency without adding currencies", async () => {
    const body: AccountsSummaryOut = { totals: [inr, usd] };
    respondWith(json(200, body));
    renderTotal();

    expect(screen.getByRole("status")).toHaveTextContent("Loading total balance...");
    expect(await screen.findByLabelText("total available INR")).toHaveTextContent(
      "INR 1,25,000.00",
    );
    expect(screen.getByLabelText("total ledger INR")).toHaveTextContent("Ledger: INR 1,57,000.00");
    expect(screen.getByLabelText("total held INR")).toHaveTextContent("On hold: INR 25,000.00");
    const inrRow = screen.getByTestId("total-INR");
    expect(within(inrRow).getByText("Available across 3 accounts")).toBeInTheDocument();
    expect(within(inrRow).getByText("1 frozen, not counted as available")).toBeInTheDocument();

    expect(screen.getByLabelText("total available USD")).toHaveTextContent("USD 7.50");
    expect(
      within(screen.getByTestId("total-USD")).getByText("Available across 1 account"),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId("total-USD")).queryByText(/frozen/)).toBeNull();
    expect(
      screen.getByText(/Available excludes amounts on hold and frozen accounts/),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/accounts/summary");
  });

  it("says so when there is nothing to total", async () => {
    respondWith(json(200, { totals: [] }));
    renderTotal();
    expect(
      await screen.findByText("No open accounts, so there is no balance to total yet."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Available excludes/)).toBeNull();
  });

  it("shows an error with a retry that loads the total", async () => {
    respondWith(
      json(500, { title: "Internal Server Error", status: 500 }),
      json(200, { totals: [inr] }),
    );
    renderTotal();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load your total balance.");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("total available INR")).toHaveTextContent(
      "INR 1,25,000.00",
    );
  });
});
