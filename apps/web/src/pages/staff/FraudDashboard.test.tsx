import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FraudStats, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import FraudDashboard, {
  AlertsPanel,
  DecisionsPanel,
  HeadlinePanel,
  RiskBandsPanel,
  RiskyPayeesPanel,
  TopReasonsPanel,
} from "./FraudDashboard";
import { DECISION_SERIES, dailyRows, formatRate, reasonLabel } from "./fraudStatsChart";

const analyst: UserOut = {
  id: "u-fa",
  email: "fraud@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const bands = (counts: number[]): FraudStats["risk_bands"] =>
  counts.map((count, i) => ({ low: i * 10, high: i === 9 ? 100 : i * 10 + 9, count }));

const empty: FraudStats = {
  window_days: 7,
  generated_at: "2026-11-04T12:00:00Z",
  scored: 0,
  totals: { ALLOW: 0, STEP_UP: 0, REVIEW: 0, BLOCK: 0 },
  hold_rate: 0,
  block_rate: 0,
  by_day: [],
  top_rules: [],
  risky_payees: [],
  model_fallbacks: 0,
  latency_p95_ms: null,
  risk_bands: bands([0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
  alerts: { OPEN: 0, CONFIRMED_FRAUD: 0, FALSE_POSITIVE: 0 },
};

const full: FraudStats = {
  ...empty,
  scored: 8,
  totals: { ALLOW: 5, STEP_UP: 1, REVIEW: 1, BLOCK: 1 },
  hold_rate: 0.125,
  block_rate: 0.125,
  by_day: [
    { day: "2026-11-01", ALLOW: 2, STEP_UP: 0, REVIEW: 0, BLOCK: 0 },
    { day: "2026-11-04", ALLOW: 3, STEP_UP: 1, REVIEW: 1, BLOCK: 1 },
  ],
  top_rules: [
    { code: "NEW_PAYEE_LARGE_AMOUNT", source: "RULE", count: 2 },
    { code: "AMOUNT_ANOMALY", source: "ANOMALY", count: 1 },
    { code: "LEGACY", source: null, count: 1 },
  ],
  risky_payees: [
    { bank_code: "PBIP", account: "••••4321", flagged_transfers: 2, customers: 2, max_risk: 91 },
  ],
  model_fallbacks: 1,
  latency_p95_ms: 12.345,
  risk_bands: bands([5, 0, 0, 1, 0, 0, 1, 0, 0, 1]),
  alerts: { OPEN: 1, CONFIRMED_FRAUD: 1, FALSE_POSITIVE: 0 },
};

function renderWith(ui: ReactElement, me: UserOut = analyst) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>{ui}</MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fraud dashboard helpers", () => {
  it("fills every UTC day of the window, so a quiet day is a zero, not a gap", () => {
    const rows = dailyRows(full);
    expect(rows.map((r) => r.day)).toEqual([
      "2026-10-28",
      "2026-10-29",
      "2026-10-30",
      "2026-10-31",
      "2026-11-01",
      "2026-11-02",
      "2026-11-03",
      "2026-11-04",
    ]);
    expect(rows[0]).toMatchObject({ scored: 0, holdPct: null, blockPct: null });
    expect(rows.at(-1)).toMatchObject({ scored: 6, holdPct: 16.7, blockPct: 16.7 });
  });

  it("formats rates and reason codes in words", () => {
    expect(formatRate(0.125)).toBe("12.5%");
    expect(formatRate(0)).toBe("0.0%");
    expect(reasonLabel("NEW_PAYEE_LARGE_AMOUNT")).toBe("New payee large amount");
  });

  it("uses four distinct palette colours for the four decisions", () => {
    expect(new Set(DECISION_SERIES.map((s) => s.color)).size).toBe(4);
    expect(DECISION_SERIES.map((s) => s.key)).toEqual(["ALLOW", "STEP_UP", "REVIEW", "BLOCK"]);
  });
});

describe("fraud dashboard panels", () => {
  it("shows the headline rates with their definitions", () => {
    renderWith(<HeadlinePanel stats={full} />);
    expect(screen.getByRole("heading", { name: "Last 7 days at a glance" })).toBeVisible();
    expect(screen.getAllByText("12.5%")).toHaveLength(2);
    expect(screen.getByText("1 payment held for an analyst.")).toBeVisible();
    expect(screen.getByText("12.3 ms")).toBeVisible();
    expect(screen.getByText(/the rules decided alone/)).toBeVisible();
  });

  it("charts decisions with a text summary, a key and a per-day table", () => {
    renderWith(<DecisionsPanel stats={full} />);
    const summary =
      "8 payments scored over 8 days: Allowed 5, Extra verification 1, Held for review 1, Blocked 1. Busiest day 2026-11-04 with 6.";
    expect(screen.getByRole("figure", { name: summary })).toBeInTheDocument();
    expect(
      screen.getByRole("figure", { name: /Hold rate 12.5% and block rate 12.5%/ }),
    ).toBeVisible();
    const key = screen.getByRole("list", { name: "Chart key" });
    expect(
      within(key)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Allowed", "Extra verification", "Held for review", "Blocked"]);
    expect(screen.getByRole("row", { name: "2026-11-04 3 1 1 1 16.7% 16.7%" })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: "2026-10-29 0 0 0 0 - -" })).toBeInTheDocument();
  });

  it("makes each chart a named keyboard stop with a description (Recharts accessibility layer)", () => {
    // jsdom has no layout: report a real size so the charts draw their SVG
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(target: Element) {
          const contentRect = { width: 400, height: 260 } as DOMRectReadOnly;
          this.callback([{ target, contentRect } as ResizeObserverEntry], this);
        }
        unobserve() {}
        disconnect() {}
      },
    );
    try {
      renderWith(
        <>
          <DecisionsPanel stats={full} />
          <RiskBandsPanel stats={full} />
          <TopReasonsPanel stats={full} />
        </>,
      );
      for (const name of [
        "Fraud decisions per day",
        "Hold and block rates per day",
        "Payments per risk band",
        "Top fraud reasons",
      ]) {
        const chart = screen.getByRole("application", { name });
        expect(chart).toHaveAttribute("tabindex", "0");
        expect(chart.querySelector("desc")?.textContent).not.toBe("");
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("charts the risk-score distribution with a table of every band", () => {
    renderWith(<RiskBandsPanel stats={full} />);
    expect(screen.getByRole("figure", { name: /0-9 5, 10-19 0, .* 90-100 1\./ })).toBeVisible();
    expect(screen.getByRole("row", { name: "90-100 1" })).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(11);
  });

  it("lists the top reasons with their source in words", () => {
    renderWith(<TopReasonsPanel stats={full} />);
    expect(
      screen.getByRole("figure", { name: /New payee large amount 2, Amount anomaly 1/ }),
    ).toBeVisible();
    expect(
      screen.getByRole("row", {
        name: "New payee large amount NEW_PAYEE_LARGE_AMOUNT Bank rule 2",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /Amount anomaly .* Unusual pattern 1/ })).toBeVisible();
    expect(screen.getByRole("row", { name: "Legacy LEGACY - 1" })).toBeInTheDocument();
  });

  it("shows alert outcomes with a link to the queue, and masked risky destinations", () => {
    renderWith(
      <>
        <AlertsPanel stats={full} />
        <RiskyPayeesPanel stats={full} />
      </>,
    );
    expect(screen.getByRole("link", { name: "Open the alert queue" })).toHaveAttribute(
      "href",
      "/staff/fraud",
    );
    expect(screen.getByText("Confirmed fraud").nextSibling).toHaveTextContent("1");
    expect(screen.getByRole("row", { name: "PBIP ••••4321 2 2 91" })).toBeVisible();
  });

  it("shows empty states instead of charts of zeros", () => {
    renderWith(
      <>
        <DecisionsPanel stats={empty} />
        <RiskBandsPanel stats={empty} />
        <TopReasonsPanel stats={empty} />
        <RiskyPayeesPanel stats={empty} />
      </>,
    );
    expect(screen.queryByRole("figure")).not.toBeInTheDocument();
    expect(screen.getByText("No fraud decisions in this period.")).toBeVisible();
    expect(screen.getByText("No reasons were recorded in this period.")).toBeVisible();
    expect(screen.getByText(/No payments to a destination were held or blocked/)).toBeVisible();
  });
});

describe("FraudDashboard page", () => {
  it("loads the 7-day window, then refetches when the period changes", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, full));
    const user = userEvent.setup();
    renderWith(<FraudDashboard />);
    expect(screen.getByText("Loading the fraud dashboard...")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Decisions over time" })).toBeVisible();
    for (const name of [
      "Risk score distribution",
      "Top reasons",
      "Alerts raised in the period",
      "Destinations behind flagged payments",
    ]) {
      expect(screen.getByRole("heading", { name })).toBeVisible();
    }
    expect(urlOf(fetchMock.mock.calls[0]?.[0] ?? "")).toContain("/fraud/stats?days=7");
    await user.selectOptions(screen.getByLabelText("Period"), "90");
    await waitFor(() => {
      expect(urlOf(fetchMock.mock.calls.at(-1)?.[0] ?? "")).toContain("/fraud/stats?days=90");
    });
  });

  it("shows an error with retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(500, { title: "boom" }))
      .mockResolvedValue(json(200, empty));
    const user = userEvent.setup();
    renderWith(<FraudDashboard />);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No fraud decisions in this period.")).toBeVisible();
  });

  it("does not call the API without the analyst or admin role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderWith(<FraudDashboard />, { ...analyst, roles: ["COMPLIANCE_OFFICER"] });
    expect(screen.getByText(/needs the Fraud Analyst or Administrator role/)).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
