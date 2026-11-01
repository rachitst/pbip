import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SecurityMonitoring as Monitoring, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import {
  FraudEventsPanel,
  HighRiskCustomersPanel,
  LoginAnomaliesPanel,
  OpenAlertsPanel,
  SecurityMonitoring,
} from "./SecurityMonitoring";

const NOW = Date.parse("2026-11-01T12:00:00Z");

const analyst: UserOut = {
  id: "u-fa",
  email: "analyst@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const CUSTOMER = "c0ffee00-1111-4222-8333-444455556666";
const USER = "a11ce000-1111-4222-8333-444455556666";
const ALERT_OPEN = "a1e70000-0000-4000-8000-000000000001";
const ALERT_DONE = "a1e70000-0000-4000-8000-000000000002";

const empty: Monitoring = {
  generated_at: "2026-11-01T11:59:30Z",
  high_risk_window_days: 30,
  high_risk_customers: [],
  open_alerts: {
    total: 0,
    by_severity: { MEDIUM: 0, HIGH: 0 },
    oldest_open_at: null,
    oldest_open_age_seconds: null,
    oldest: [],
  },
  login_anomalies: {
    window_hours: 24,
    burst_threshold: 3,
    counts: {
      LOGIN_FAILURE: 0,
      ACCOUNT_LOCKED: 0,
      LOGIN_BLOCKED_LOCKED: 0,
      MFA_FAILURE: 0,
      STEP_UP_FAILURE: 0,
    },
    unknown_account_failures: 0,
    bursts: [],
    recent_lockouts: [],
  },
  fraud_events: { last_24h: { REVIEW: 0, BLOCK: 0 }, latest: [] },
};

const full: Monitoring = {
  ...empty,
  high_risk_customers: [
    {
      customer_id: CUSTOMER,
      flagged_events: 3,
      blocked: 1,
      max_risk: 95,
      open_alerts: 2,
      last_flagged_at: "2026-11-01T11:00:00Z",
      alert_id: ALERT_OPEN,
    },
    {
      customer_id: "b0b00000-1111-4222-8333-444455556666",
      flagged_events: 1,
      blocked: 0,
      max_risk: 72,
      open_alerts: 0,
      last_flagged_at: "2026-10-30T12:00:00Z",
      alert_id: ALERT_DONE,
    },
  ],
  open_alerts: {
    total: 3,
    by_severity: { MEDIUM: 2, HIGH: 1 },
    oldest_open_at: "2026-11-01T09:00:00Z",
    oldest_open_age_seconds: 3 * 3600,
    oldest: [
      {
        alert_id: ALERT_OPEN,
        severity: "HIGH",
        risk_score: 95,
        created_at: "2026-11-01T09:00:00Z",
      },
    ],
  },
  login_anomalies: {
    ...empty.login_anomalies,
    counts: { ...empty.login_anomalies.counts, LOGIN_FAILURE: 12, ACCOUNT_LOCKED: 1 },
    unknown_account_failures: 2,
    bursts: [{ user_id: USER, failures: 5, last_failure_at: "2026-11-01T11:59:00Z" }],
    recent_lockouts: [
      { user_id: USER, locked_at: "2026-11-01T11:15:00Z" },
      { user_id: null, locked_at: "2026-10-31T12:00:00Z" },
    ],
  },
  fraud_events: {
    last_24h: { REVIEW: 1, BLOCK: 1 },
    latest: [
      {
        event_id: "e1",
        created_at: "2026-11-01T11:59:50Z",
        decision: "BLOCK",
        risk_score: 95,
        customer_id: CUSTOMER,
        alert_id: ALERT_OPEN,
        alert_status: "OPEN",
        amount_minor: 1_000_000,
        currency: "INR",
        reason_codes: ["NEW_PAYEE_LARGE_AMOUNT"],
      },
      {
        event_id: "e2",
        created_at: "2026-10-31T12:00:00Z",
        decision: "STEP_UP",
        risk_score: 45,
        customer_id: CUSTOMER,
        alert_id: null,
        alert_status: null,
        amount_minor: 5_000,
        currency: "INR",
        reason_codes: [],
      },
    ],
  },
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

const rowOf = (el: HTMLElement) => {
  const row = el.closest("tr");
  if (!row) throw new Error("not in a table row");
  return within(row);
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] }); // relative times are read against a fixed "now"
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Open investigations panel", () => {
  it("counts open alerts by severity, says how long the oldest waited and links to it", () => {
    renderWith(<OpenAlertsPanel panel={full.open_alerts} />);
    expect(screen.getByText("3 open alerts")).toBeInTheDocument();
    expect(screen.getByText("3 hours")).toBeInTheDocument();
    expect(screen.getByLabelText("High severity: 1 open alert of 3")).toHaveAttribute("value", "1");
    expect(screen.getByLabelText("Medium severity: 2 open alerts of 3")).toHaveAttribute(
      "value",
      "2",
    );
    expect(screen.getByRole("link", { name: "3 hours ago" })).toHaveAttribute(
      "href",
      `/staff/fraud/${ALERT_OPEN}`,
    );
    expect(screen.getByRole("link", { name: "Open the alert queue" })).toHaveAttribute(
      "href",
      "/staff/fraud",
    );
  });

  it("uses the singular for one alert and has an empty state", () => {
    const one = {
      ...full.open_alerts,
      total: 1,
      by_severity: { MEDIUM: 1, HIGH: 0 },
      oldest_open_age_seconds: 3600,
    };
    const { unmount } = renderWith(<OpenAlertsPanel panel={one} />);
    expect(screen.getByText("1 open alert")).toBeInTheDocument();
    expect(screen.getByText("1 hour")).toBeInTheDocument();
    unmount();
    renderWith(<OpenAlertsPanel panel={empty.open_alerts} />);
    expect(screen.getByText(/No open alerts/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("High-risk customers panel", () => {
  it("lists customers by id with counts, risk and a drill-down to their alert", () => {
    renderWith(<HighRiskCustomersPanel customers={full.high_risk_customers} windowDays={30} />);
    const top = rowOf(screen.getByTitle(CUSTOMER));
    expect(top.getByText("c0ffee00")).toBeInTheDocument();
    expect(top.getByText("95")).toBeInTheDocument();
    expect(top.getByText("1 hour ago")).toBeInTheDocument();
    expect(top.getByRole("link", { name: /Open the alert for customer c0ffee00/ })).toHaveAttribute(
      "href",
      `/staff/fraud/${ALERT_OPEN}`,
    );
    expect(top.getByText("Review")).toBeInTheDocument(); // still has open alerts
    const second = rowOf(screen.getByText("b0b00000"));
    expect(second.getByText("View")).toBeInTheDocument(); // nothing left open
    expect(second.getByText("2 days ago")).toBeInTheDocument();
  });

  it("has an empty state that names the window", () => {
    renderWith(<HighRiskCustomersPanel customers={[]} windowDays={30} />);
    expect(screen.getByText("No flagged payments in the last 30 days.")).toBeInTheDocument();
  });
});

describe("Login anomalies panel", () => {
  it("shows the 24-hour counts, bursts and lockouts with user ids only", () => {
    renderWith(<LoginAnomaliesPanel panel={full.login_anomalies} />);
    expect(screen.getByText("Last 24 hours. User ids only; no addresses or emails.")).toBeVisible();
    expect(screen.getByText("Failed sign-ins").nextSibling).toHaveTextContent("12");
    expect(screen.getByText("Failed sign-ins for unknown emails").nextSibling).toHaveTextContent(
      "2",
    );
    const burst = rowOf(screen.getByText("1 minute ago"));
    expect(burst.getByText("a11ce000")).toBeInTheDocument();
    expect(burst.getByText("5")).toBeInTheDocument();
    expect(rowOf(screen.getByText("45 minutes ago")).getByText("a11ce000")).toBeInTheDocument();
    expect(rowOf(screen.getByText("yesterday")).getByText("Deleted user")).toBeInTheDocument();
  });

  it("has empty states for bursts and lockouts", () => {
    renderWith(<LoginAnomaliesPanel panel={empty.login_anomalies} />);
    expect(
      screen.getByText("No user had 3 or more failed sign-ins in the last 24 hours."),
    ).toBeInTheDocument();
    expect(screen.getByText("No accounts have been locked.")).toBeInTheDocument();
  });
});

describe("Recent fraud events panel", () => {
  it("lists the newest flagged decisions with links to their alerts", () => {
    renderWith(<FraudEventsPanel panel={full.fraud_events} />);
    expect(screen.getByText("1 payment held for review")).toBeInTheDocument();
    const blocked = rowOf(screen.getByRole("link", { name: "just now" }));
    expect(screen.getByRole("link", { name: "just now" })).toHaveAttribute(
      "href",
      `/staff/fraud/${ALERT_OPEN}`,
    );
    expect(blocked.getByText("Blocked")).toBeInTheDocument();
    expect(blocked.getByText("Open")).toBeInTheDocument();
    const noAlert = rowOf(screen.getByText("yesterday"));
    expect(noAlert.getByText("Extra verification")).toBeInTheDocument();
    expect(noAlert.getByText("No alert")).toBeInTheDocument();
    expect(noAlert.queryByRole("link")).not.toBeInTheDocument();
  });

  it("pluralises the 24-hour count and has an empty state", () => {
    renderWith(
      <FraudEventsPanel panel={{ ...empty.fraud_events, last_24h: { REVIEW: 2, BLOCK: 0 } }} />,
    );
    expect(screen.getByText("2 payments held for review")).toBeInTheDocument();
    expect(screen.getByText("No held, blocked or escalated payments yet.")).toBeInTheDocument();
  });
});

describe("SecurityMonitoring page", () => {
  it("loads the panels from GET /fraud/monitoring", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, full));
    renderWith(<SecurityMonitoring />);
    expect(screen.getByText("Loading security monitoring...")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Open investigations" })).toBeVisible();
    for (const name of ["High-risk customers", "Login anomalies", "Recent fraud events"]) {
      expect(screen.getByRole("heading", { name })).toBeVisible();
    }
    expect(screen.getByText("Updated just now")).toBeInTheDocument();
    expect(urlOf(fetchMock.mock.calls[0]?.[0] ?? "")).toContain("/api/v1/fraud/monitoring");
  });

  it("shows an error with retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(500, { title: "boom" }))
      .mockResolvedValue(json(200, empty));
    renderWith(<SecurityMonitoring />);
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/No open alerts/)).toBeInTheDocument();
  });

  it("does not call the API for staff without the analyst role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderWith(<SecurityMonitoring />, { ...analyst, roles: ["COMPLIANCE_OFFICER"] });
    expect(screen.getByText(/needs the Fraud Analyst or Administrator role/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
