import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { customerMe } from "../test-utils";

function urlOf(input: Parameters<typeof fetch>[0]): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const mockAccount = {
  id: "acc-1",
  account_number: "409443369528",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 1_000_000,
  held_minor: 0,
  available_minor: 1_000_000,
};

const mockAccount2 = {
  id: "acc-2",
  account_number: "409443369599",
  account_number_masked: "••••••••9599",
  account_type: "CURRENT",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 500_000,
  held_minor: 0,
  available_minor: 500_000,
};

const mockSpendingData = {
  account_id: "acc-1",
  currency: "INR",
  period: "2026-10",
  year: 2026,
  month: 10,
  total_spend_minor: 125_000, // INR 1,250.00
  transaction_count: 5,
  categories: [
    {
      category: "Groceries",
      amount_minor: 75_000,
      percentage: 60.0,
      transaction_count: 3,
    },
    {
      category: "Utilities",
      amount_minor: 50_000,
      percentage: 40.0,
      transaction_count: 2,
    },
  ],
};

const mockTrendsData = {
  account_id: "acc-1",
  currency: "INR",
  current_month: {
    period: "2026-10",
    year: 2026,
    month: 10,
    total_spend_minor: 125_000,
    transaction_count: 5,
    daily_burn_rate_minor: 4_032,
  },
  previous_month: {
    period: "2026-09",
    year: 2026,
    month: 9,
    total_spend_minor: 100_000,
    transaction_count: 4,
    daily_burn_rate_minor: 3_333,
  },
  mom_spend_change_percentage: 25.0,
  daily_burn_rate_minor: 4_032,
  burn_rate_change_percentage: 21.0,
  top_categories_increasing: [
    {
      category: "Groceries",
      current_amount_minor: 75_000,
      previous_amount_minor: 50_000,
      change_minor: 25_000,
      change_percentage: 50.0,
    },
  ],
  top_categories_decreasing: [
    {
      category: "Dining",
      current_amount_minor: 0,
      previous_amount_minor: 15_000,
      change_minor: -15_000,
      change_percentage: -100.0,
    },
  ],
};

const mockHealthData = {
  account_id: "acc-1",
  currency: "INR",
  overall_score: 75,
  rating: "GOOD",
  insufficient_data: false,
  months_observed: 6,
  window_months: 6,
  components: {
    savings_rate: {
      score: 24,
      max_score: 30,
      weight_percentage: 30.0,
      metric_value: 0.16,
      formatted_value: "16.0%",
      assessment: "MODERATE",
    },
    buffer_months: {
      score: 16,
      max_score: 20,
      weight_percentage: 20.0,
      metric_value: 3.2,
      formatted_value: "3.2 months",
      assessment: "ADEQUATE",
    },
    emi_to_income: {
      score: 20,
      max_score: 25,
      weight_percentage: 25.0,
      metric_value: 0.15,
      formatted_value: "15.0%",
      assessment: "MODERATE",
    },
    expense_volatility: {
      score: 15,
      max_score: 25,
      weight_percentage: 25.0,
      metric_value: 0.35,
      formatted_value: "0.35 CV",
      assessment: "VOLATILE",
    },
  },
  recommendations: [
    "Increase monthly savings contributions to reach the 20% target.",
    "Maintain cash reserves for at least 6 months of living expenses.",
  ],
};

function renderInsightsApp(initialPath = "/insights") {
  window.history.replaceState(null, "", initialPath);
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AuthProvider>
        <App />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("Customer Financial Analytics UI (TASK-049)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("1. Renders customer navigation link and navigates to /insights", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/accounts");

    const navLink = await screen.findByRole("link", { name: "Spending & health" });
    expect(navLink).toBeInTheDocument();
    expect(navLink).toHaveAttribute("href", "/insights");

    await userEvent.click(navLink);

    expect(
      await screen.findByRole("heading", { name: "Spending & Financial Health", level: 1 }),
    ).toBeInTheDocument();
  });

  it("2. Handles loading state while requests are pending", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      // Pending response
      return new Promise(() => {});
    });

    renderInsightsApp("/insights");

    expect(
      await screen.findByText("Loading spending & financial health insights..."),
    ).toBeInTheDocument();
  });

  it("3. Displays financial health score with Savings, Buffer, and Debt progress indicators", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    await screen.findByRole("heading", { name: "Financial Health Score" });

    // Overall score
    expect(screen.getByText("75")).toBeInTheDocument();
    expect(screen.getByText("GOOD")).toBeInTheDocument();
    expect(
      screen.getByText(/Computed from 6 observed months of banking history/),
    ).toBeInTheDocument();

    // Savings Component
    expect(screen.getByText("24 / 30 pts")).toBeInTheDocument();
    expect(screen.getByText("16.0%")).toBeInTheDocument();
    const savingsBar = screen.getByRole("progressbar", { name: "Savings score" });
    expect(savingsBar).toHaveAttribute("value", "24");
    expect(savingsBar).toHaveAttribute("max", "30");

    // Buffer Component
    expect(screen.getByText("16 / 20 pts")).toBeInTheDocument();
    expect(screen.getByText("3.2 months")).toBeInTheDocument();
    const bufferBar = screen.getByRole("progressbar", { name: "Cash buffer score" });
    expect(bufferBar).toHaveAttribute("value", "16");
    expect(bufferBar).toHaveAttribute("max", "20");

    // Debt Component
    expect(screen.getByText("20 / 25 pts")).toBeInTheDocument();
    expect(screen.getByText("15.0%")).toBeInTheDocument();
    const debtBar = screen.getByRole("progressbar", { name: "Debt repayments score" });
    expect(debtBar).toHaveAttribute("value", "20");
    expect(debtBar).toHaveAttribute("max", "25");

    // Recommendations
    expect(screen.getByText(/Increase monthly savings contributions/)).toBeInTheDocument();
  });

  it("4. Displays category spending donut chart, amounts, percentages and accessible table fallback", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    await screen.findByRole("heading", { name: "Spending by Category" });

    // Region for donut chart
    expect(
      screen.getByRole("region", { name: "Category spending distribution donut chart" }),
    ).toBeInTheDocument();

    // Summary line
    expect(
      screen.getByText(/Total spending of INR 1,250.00 across 2 categories in 2026-10/),
    ).toBeInTheDocument();

    // Category elements
    expect(screen.getAllByText("Groceries")[0]).toBeInTheDocument();
    expect(screen.getAllByText("60.0%")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Utilities")[0]).toBeInTheDocument();
    expect(screen.getAllByText("40.0%")[0]).toBeInTheDocument();

    // Accessible table fallback
    const table = screen.getByRole("table", { name: "Category spending breakdown table" });
    expect(table).toBeInTheDocument();
    expect(within(table).getByText("Groceries")).toBeInTheDocument();
    expect(within(table).getByText("60.0%")).toBeInTheDocument();
  });

  it("5. Displays month-over-month trends, comparison bar chart, delta badges, and burn rate", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    await screen.findByRole("heading", { name: "Month-over-Month Trends" });

    // Bar chart region
    expect(
      screen.getByRole("region", { name: "Month-over-month spending comparison bar chart" }),
    ).toBeInTheDocument();

    // Delta indicator
    expect(screen.getByText(/▲ Increased by 25.0%/)).toBeInTheDocument();

    // Burn rate
    expect(screen.getAllByText(/Daily Burn Rate/)[0]).toBeInTheDocument();
    expect(screen.getByText(/▲ \+21.0% vs previous/)).toBeInTheDocument();

    // Category shifts
    expect(screen.getByText("Top Spending Increases")).toBeInTheDocument();
    expect(screen.getByText("Top Spending Decreases")).toBeInTheDocument();

    // Accessible monthly comparison table
    const table = screen.getByRole("table", { name: "Month-over-month comparison table" });
    expect(table).toBeInTheDocument();
    expect(within(table).getByText(/2026-10 \(Current\)/)).toBeInTheDocument();
  });

  it("6. Handles empty category spending data safely without crashing", async () => {
    const emptySpending = {
      account_id: "acc-1",
      currency: "INR",
      period: "2026-10",
      year: 2026,
      month: 10,
      total_spend_minor: 0,
      transaction_count: 0,
      categories: [],
    };

    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, emptySpending));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    expect(
      await screen.findByText("No categorized debit transactions recorded for this period."),
    ).toBeInTheDocument();
  });

  it("7. Handles empty accounts state", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, []));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    expect(await screen.findByText("You have no accounts yet.")).toBeInTheDocument();
  });

  it("8. Handles API error and supports retry", async () => {
    let shouldFail = true;
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) {
        if (shouldFail) {
          return Promise.resolve(
            new Response(
              JSON.stringify({ code: "analytics_error", title: "Analytics service unavailable" }),
              { status: 400, headers: { "Content-Type": "application/problem+json" } },
            ),
          );
        }
        return Promise.resolve(json(200, mockSpendingData));
      }
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    const errorAlert = await screen.findByRole("alert");
    expect(errorAlert).toHaveTextContent("Analytics service unavailable");

    // Click retry
    shouldFail = false;
    const retryBtn = within(errorAlert).getByRole("button", { name: "Try again" });
    await userEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Spending by Category" })).toBeInTheDocument();
    });
  });

  it("9. Supports multiple accounts and account switching", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount, mockAccount2]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, mockTrendsData));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    const selector = await screen.findByLabelText("Account:");
    expect(selector).toBeInTheDocument();
    expect(selector).toHaveValue("acc-1");

    await userEvent.selectOptions(selector, "acc-2");
    expect(selector).toHaveValue("acc-2");
  });

  it("10. Displays clean empty state when no prior month data exists (single-month history)", async () => {
    const singleMonthTrends = {
      ...mockTrendsData,
      previous_month: null,
      mom_spend_change_percentage: null,
      burn_rate_change_percentage: null,
      top_categories_increasing: [],
      top_categories_decreasing: [],
    };

    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, singleMonthTrends));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    await screen.findByRole("heading", { name: "Month-over-Month Trends" });

    // Bar chart should NOT be rendered
    expect(
      screen.queryByRole("region", { name: "Month-over-month spending comparison bar chart" }),
    ).not.toBeInTheDocument();

    // Clean accessible empty state message should be displayed
    expect(
      screen.getByText(
        "No month-over-month comparison data available. Additional transaction history is required to display spending trends.",
      ),
    ).toBeInTheDocument();

    // Summary cards must remain visible
    expect(screen.getAllByText("Daily Burn Rate")[0]).toBeInTheDocument();
    expect(screen.getByText(/Current Period \(2026-10\)/)).toBeInTheDocument();
    expect(screen.getByText(/Previous Period \(N\/A\)/)).toBeInTheDocument();

    // Comparison table has meaningful column headers and an informative empty row
    const table = screen.getByRole("table", { name: "Month-over-month comparison table" });
    expect(within(table).getByRole("columnheader", { name: "Period" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Total Spend" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Transactions" })).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: "Daily Burn Rate" }),
    ).toBeInTheDocument();
    expect(
      within(table).getByText("No prior month data available for comparison."),
    ).toBeInTheDocument();
  });

  it("11. Displays clean empty state when both comparison periods have zero spend", async () => {
    const zeroSpendTrends = {
      ...mockTrendsData,
      current_month: {
        ...mockTrendsData.current_month,
        total_spend_minor: 0,
        transaction_count: 0,
        daily_burn_rate_minor: 0,
      },
      previous_month: {
        ...mockTrendsData.previous_month,
        total_spend_minor: 0,
        transaction_count: 0,
        daily_burn_rate_minor: 0,
      },
      mom_spend_change_percentage: null,
      burn_rate_change_percentage: null,
      top_categories_increasing: [],
      top_categories_decreasing: [],
    };

    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [mockAccount]));
      if (url.includes("/analytics/spending")) return Promise.resolve(json(200, mockSpendingData));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, zeroSpendTrends));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, mockHealthData));
      return Promise.resolve(json(404, { detail: "not found" }));
    });

    renderInsightsApp("/insights");

    await screen.findByRole("heading", { name: "Month-over-Month Trends" });

    // Bar chart should NOT be rendered
    expect(
      screen.queryByRole("region", { name: "Month-over-month spending comparison bar chart" }),
    ).not.toBeInTheDocument();

    // Empty state message displayed
    expect(
      screen.getByText(
        "No month-over-month comparison data available. Additional transaction history is required to display spending trends.",
      ),
    ).toBeInTheDocument();

    // Comparison table has meaningful column headers and zero spending empty message
    const table = screen.getByRole("table", { name: "Month-over-month comparison table" });
    expect(within(table).getByRole("columnheader", { name: "Period" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Total Spend" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Transactions" })).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: "Daily Burn Rate" }),
    ).toBeInTheDocument();
    expect(
      within(table).getByText("No spending recorded across comparison periods."),
    ).toBeInTheDocument();
  });
});
