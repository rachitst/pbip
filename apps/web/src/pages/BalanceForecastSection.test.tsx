import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { BalanceForecastResponse } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { customerMe } from "../test-utils";
import { ForecastTooltip } from "./BalanceForecastSection";
import { axisAmount, forecastRows, forecastSummary, obligationLabel } from "./forecastChart";
// Loaded lazily by the route; importing it warms the module cache (see Insights.test.tsx).
import "./Insights";

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

const account = {
  id: "acc-1",
  account_number: "409443369528",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 50_000_00,
  held_minor: 0,
  available_minor: 50_000_00,
};

const point = (date: string, predicted: number, spread: number, due = 0) => ({
  date,
  predicted_minor: predicted,
  lower_80_minor: predicted - spread,
  upper_80_minor: predicted + spread,
  lower_95_minor: predicted - 2 * spread,
  upper_95_minor: predicted + 2 * spread,
  obligations_minor: due,
});

function forecast(overrides: Partial<BalanceForecastResponse> = {}): BalanceForecastResponse {
  return {
    label: "Predicted, not exact",
    account_id: "acc-1",
    currency: "INR",
    method: "auto_arima",
    model_version: "forecast-arima-1",
    history_days: 60,
    horizon_days: 3,
    history: [
      { date: "2026-10-31", balance_minor: 52_000_00 },
      { date: "2026-11-01", balance_minor: 50_000_00 },
    ],
    points: [
      point("2026-11-02", 49_500_00, 1_000_00),
      point("2026-11-03", 34_500_00, 1_500_00, 15_000_00),
      point("2026-11-04", 34_000_00, 2_000_00),
    ],
    obligations: [
      {
        date: "2026-11-03",
        amount_minor: 15_000_00,
        kind: "SCHEDULED_TRANSFER",
        scheduled_transfer_id: "st-1",
        loan_id: null,
        reference: "Rent",
      },
      {
        date: "2026-11-05",
        amount_minor: 8_862_00,
        kind: "LOAN_EMI",
        scheduled_transfer_id: "st-2",
        loan_id: "loan-1",
        reference: null,
      },
    ],
    end_of_month: {
      date: "2026-11-30",
      predicted_minor: 20_000_00,
      lower_80_minor: 15_000_00,
      upper_80_minor: 25_000_00,
      lower_95_minor: 12_000_00,
      upper_95_minor: 28_000_00,
      obligations_minor: 23_862_00,
      alert_threshold_minor: 1_000_00,
      low_balance: false,
    },
    ...overrides,
  };
}

const analytics = {
  spending: {
    account_id: "acc-1",
    currency: "INR",
    period: "2026-11",
    year: 2026,
    month: 11,
    total_spend_minor: 0,
    transaction_count: 0,
    categories: [],
  },
  trends: {
    account_id: "acc-1",
    currency: "INR",
    current_month: {
      period: "2026-11",
      year: 2026,
      month: 11,
      total_spend_minor: 0,
      transaction_count: 0,
      daily_burn_rate_minor: 0,
    },
    previous_month: null,
    mom_spend_change_percentage: null,
    daily_burn_rate_minor: 0,
    burn_rate_change_percentage: null,
    top_categories_increasing: [],
    top_categories_decreasing: [],
  },
  health: {
    account_id: "acc-1",
    currency: "INR",
    overall_score: 70,
    rating: "GOOD",
    insufficient_data: false,
    months_observed: 6,
    window_months: 6,
    components: Object.fromEntries(
      ["savings_rate", "buffer_months", "emi_to_income", "expense_volatility"].map((key) => [
        key,
        {
          score: 10,
          max_score: 25,
          weight_percentage: 25,
          metric_value: 0.1,
          formatted_value: "10%",
          assessment: "MODERATE",
        },
      ]),
    ),
    recommendations: [],
  },
};

function mockApi(
  fetchMock: ReturnType<typeof vi.fn<typeof fetch>>,
  forecastResponse: () => Response,
) {
  fetchMock.mockImplementation((input) => {
    const url = urlOf(input);
    if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
    if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
    if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
    if (url.includes("/analytics/spending")) return Promise.resolve(json(200, analytics.spending));
    if (url.includes("/analytics/trends")) return Promise.resolve(json(200, analytics.trends));
    if (url.includes("/analytics/health-score"))
      return Promise.resolve(json(200, analytics.health));
    if (url.includes("/analytics/forecast")) return Promise.resolve(forecastResponse());
    if (url.includes("/notifications"))
      return Promise.resolve(json(200, { items: [], total: 0, unread_count: 0 }));
    return Promise.resolve(json(404, { detail: "not found" }));
  });
}

function renderInsights() {
  window.history.replaceState(null, "", "/insights");
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

async function forecastSection() {
  const heading = await screen.findByRole("heading", { name: "Balance Forecast" });
  const section = heading.closest("section");
  if (!section) throw new Error("the forecast heading is not inside its section");
  return section;
}

describe("Balance forecast chart (TASK-050)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the projection labelled as predicted, its month end, payments and a table", async () => {
    const warn = vi.spyOn(console, "warn");
    const error = vi.spyOn(console, "error");
    mockApi(fetchMock, () => json(200, forecast()));
    renderInsights();
    const section = within(await forecastSection());

    expect(await section.findByText("Predicted, not exact")).toBeInTheDocument();
    expect(
      section.getByRole("region", { name: "Balance history and 30-day forecast chart" }),
    ).toBeInTheDocument();
    expect(section.getByText(/Projected on 30 Nov \(month end\)/)).toBeInTheDocument();
    expect(section.getByText("80% range INR 15,000.00 to INR 25,000.00")).toBeInTheDocument();
    expect(
      section.getByText(/This includes INR 23,862.00 of scheduled payments due before/),
    ).toBeInTheDocument();

    const payments = section.getByRole("list", { name: "Scheduled payments in the projection" });
    expect(within(payments).getByText("Scheduled transfer: Rent")).toBeInTheDocument();
    expect(within(payments).getByText("Loan EMI")).toBeInTheDocument();
    expect(within(payments).getByText("3 Nov")).toBeInTheDocument();

    const key = section.getByRole("list", { name: "Chart key" });
    expect(within(key).getByText(/80% and 95% prediction ranges/)).toBeInTheDocument();
    expect(within(key).getByText(/Scheduled/)).toBeInTheDocument();

    await userEvent.click(section.getByText("View forecast as a table"));
    const rows = within(
      section.getByRole("table", { name: "Balance forecast table" }),
    ).getAllByRole("row");
    expect(rows).toHaveLength(4); // header + three predicted days
    expect(within(rows[2] as HTMLElement).getByText("INR 15,000.00")).toBeInTheDocument();
    expect(section.queryByRole("status", { name: /low/i })).not.toBeInTheDocument();
    expect(section.queryByText(/may run low/)).not.toBeInTheDocument();
    // jsdom has no layout, so Recharts' size warning is the only one allowed (a browser has one).
    const warnings = warn.mock.calls.filter(
      ([message]) => !String(message).includes("of chart should be greater than 0"),
    );
    expect(warnings).toEqual([]);
    expect(error).not.toHaveBeenCalled();
  });

  it("warns when the month may end low", async () => {
    const low = forecast();
    mockApi(fetchMock, () =>
      json(200, {
        ...low,
        end_of_month: { ...low.end_of_month, predicted_minor: 500_00, low_balance: true },
      }),
    );
    renderInsights();
    const section = within(await forecastSection());
    expect(
      await section.findByText(/Your balance may run low by 30 Nov: about INR 500.00/),
    ).toBeInTheDocument();
  });

  it("renders when the interval has no width and there are no payments", async () => {
    const flat = forecast({
      points: [point("2026-11-02", 50_000_00, 0), point("2026-11-03", 50_000_00, 0)],
      obligations: [],
      end_of_month: { ...forecast().end_of_month, obligations_minor: 0 },
    });
    mockApi(fetchMock, () => json(200, flat));
    renderInsights();
    const section = within(await forecastSection());
    expect(
      await section.findByText("No scheduled payments in the next 30 days."),
    ).toBeInTheDocument();
    expect(section.queryByText(/This includes/)).not.toBeInTheDocument();
  });

  it("shows the loading state while the forecast is computed", async () => {
    mockApi(fetchMock, () => json(200, forecast()));
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.includes("/analytics/forecast")) return new Promise(() => {});
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "tok" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.includes("/analytics/spending"))
        return Promise.resolve(json(200, analytics.spending));
      if (url.includes("/analytics/trends")) return Promise.resolve(json(200, analytics.trends));
      if (url.includes("/analytics/health-score"))
        return Promise.resolve(json(200, analytics.health));
      return Promise.resolve(json(200, { items: [], total: 0, unread_count: 0 }));
    });
    renderInsights();
    const section = within(await forecastSection());
    expect(section.getByText("Projecting your balance...")).toBeInTheDocument();
  });

  it("explains a missing history as an empty state, not an error", async () => {
    mockApi(fetchMock, () =>
      json(422, { title: "Unprocessable", status: 422, detail: "at least two days" }),
    );
    renderInsights();
    const section = within(await forecastSection());
    expect(
      await section.findByText(/Not enough account history to forecast yet/),
    ).toBeInTheDocument();
    expect(section.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an error with a retry that loads the forecast", async () => {
    let calls = 0;
    mockApi(fetchMock, () => {
      calls += 1;
      return calls === 1
        ? json(500, { title: "Internal Server Error", status: 500 })
        : json(200, forecast());
    });
    renderInsights();
    const section = within(await forecastSection());
    expect(await section.findByRole("alert")).toBeInTheDocument();
    await userEvent.click(section.getByRole("button", { name: "Try again" }));
    expect(await section.findByText(/Projected on 30 Nov/)).toBeInTheDocument();
  });
});

describe("forecast chart data (TASK-050)", () => {
  it("continues the history into the forecast and marks the payment days", () => {
    const rows = forecastRows(forecast());
    expect(rows.map((row) => row.date)).toEqual([
      "2026-10-31",
      "2026-11-01",
      "2026-11-02",
      "2026-11-03",
      "2026-11-04",
    ]);
    // The last actual day also starts the predicted line: no gap between the two.
    expect(rows[1]).toMatchObject({ actual: 50_000_00, predicted: 50_000_00, band80: null });
    expect(rows[2]).toMatchObject({ actual: null, band80: [48_500_00, 50_500_00] });
    expect(rows.map((row) => row.paymentMarker)).toEqual([null, null, null, 34_500_00, null]);
    expect(rows[3]?.payments.map(obligationLabel)).toEqual(["Scheduled transfer: Rent"]);
  });

  it("summarises the chart in text", () => {
    expect(forecastSummary(forecast())).toBe(
      "Predicted, not exact: by 30 Nov the balance is projected at about INR 20,000.00 " +
        "(80% range INR 15,000.00 to INR 25,000.00). This includes INR 23,862.00 of scheduled " +
        "payments due before the month end. In 3 days (4 Nov) it is projected at about " +
        "INR 34,000.00.",
    );
  });

  it("labels the axis compactly", () => {
    expect([axisAmount(50_000_00), axisAmount(12_50_000_00), axisAmount(500_00)]).toEqual([
      "50k",
      "12.5L",
      "500",
    ]);
  });

  it("tooltip shows the date, predicted balance, bounds and the day's payments", () => {
    const row = forecastRows(forecast())[3];
    render(<ForecastTooltip row={row} currency="INR" />);
    expect(screen.getByText("3 Nov")).toBeInTheDocument();
    expect(screen.getByText("Predicted balance: INR 34,500.00")).toBeInTheDocument();
    expect(screen.getByText("80% range: INR 33,000.00 to INR 36,000.00")).toBeInTheDocument();
    expect(screen.getByText("Scheduled transfer: Rent: −INR 15,000.00")).toBeInTheDocument();
    expect(screen.getByText("Predicted, not exact")).toBeInTheDocument();
  });

  it("tooltip on a past day shows the actual balance and no range", () => {
    render(<ForecastTooltip row={forecastRows(forecast())[0]} currency="INR" />);
    expect(screen.getByText("Actual balance: INR 52,000.00")).toBeInTheDocument();
    expect(screen.queryByText(/range/)).not.toBeInTheDocument();
  });
});
