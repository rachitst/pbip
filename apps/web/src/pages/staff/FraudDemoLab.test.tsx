import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertDetail, AlertSummary, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { FraudDemoLab } from "./FraudDemoLab";
import {
  WRONG_PASSWORD_ATTEMPT,
  generateIdempotencyKey,
  secureRandom4Digits,
} from "./fraudLabConfig";

const analyst: UserOut = {
  id: "u-fa",
  email: "analyst@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const customerOnly: UserOut = {
  id: "u-cu",
  email: "alice@demo.pbip",
  roles: ["CUSTOMER"],
  mfa_enabled: false,
  mfa_verified_session: false,
  created_at: "2026-01-01T00:00:00Z",
};

const summary: AlertSummary = {
  id: "al-lab-1",
  status: "OPEN",
  severity: "HIGH",
  risk_score: 87,
  decision: "REVIEW",
  amount_minor: 3_000_000,
  currency: "INR",
  transaction_status: "PENDING_REVIEW",
  created_at: "2026-11-01T12:00:00Z",
};

const detail: AlertDetail = {
  ...summary,
  transaction_id: "tx-lab-1",
  customer_id: "cu-alice-1",
  customer: {
    full_name: "Alice Demo",
    kyc_status: "VERIFIED",
    segment: "salaried",
    risk_profile: null,
    customer_since: "2025-01-01T00:00:00Z",
  },
  transaction_type: "TRANSFER",
  reference: "Simulated attack F1",
  reasons: [
    {
      code: "NEW_PAYEE_LARGE_AMOUNT",
      message: "Payee added 3m ago and INR 30,000.00 is large",
      source: "RULE",
      contribution: 45,
    },
    {
      code: "MODEL_PAYEE_AGE_SECONDS",
      message: "Model: payee was added 3 minutes before the transfer",
      source: "MODEL",
      contribution: 2.4,
    },
    {
      code: "ANOMALY",
      message: "Unusual: ratio_to_avg_amount is 9.00, usually about 1.00",
      source: "ANOMALY",
      contribution: 0.85,
    },
  ],
  features: { payee_age_seconds: 180, ratio_to_avg_amount: 9.0 },
  model_version: "hybrid-lgbm-iforest-1",
  latency_ms: 19.2,
  resolution_note: null,
  resolved_at: null,
};

function renderLab(me: UserOut = analyst): ReturnType<typeof render> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter initialEntries={["/staff/fraud-lab"]}>
          <Routes>
            <Route path="/staff/fraud-lab" element={<FraudDemoLab />} />
            <Route
              path="/staff/fraud/:alertId"
              element={<div data-testid="alert-page">Alert Detail Page</div>}
            />
          </Routes>
        </MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

describe("FraudDemoLab (TASK-081)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const originalAppEnv = process.env.APP_ENV;

  beforeEach(() => {
    process.env.APP_ENV = "local";
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);

    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/auth/login") && method === "POST") {
        const body = JSON.parse((init?.body as string) || "{}") as {
          email?: string;
          password?: string;
        };
        if (body.password === WRONG_PASSWORD_ATTEMPT) {
          // pragma: allowlist secret
          return Promise.resolve(
            json(401, { code: "INVALID_CREDENTIALS", title: "Invalid password" }),
          );
        }
        return Promise.resolve(json(200, { accessToken: "tok-victim-alice" }));
      }

      if (url.endsWith("/api/v1/accounts") && method === "GET") {
        return Promise.resolve(
          json(200, [
            {
              id: "acc-alice-01",
              account_number: "409443369528",
              ledger_balance_minor: 25_000_000,
            },
          ]),
        );
      }

      if (url.endsWith("/api/v1/payees") && method === "POST") {
        return Promise.resolve(
          json(201, {
            id: "payee-sim-01",
            name: "Investment Scam Co",
            account_number: "999900001111",
          }),
        );
      }

      if (url.includes("/api/v1/payees/") && url.endsWith("/verify") && method === "POST") {
        return Promise.resolve(json(200, { id: "payee-sim-01", verified: true }));
      }

      if (url.endsWith("/api/v1/transfers") && method === "POST") {
        const body = JSON.parse((init?.body as string) || "{}") as { amount_minor: number };
        return Promise.resolve(
          json(202, {
            id: "tx-lab-1",
            status: "PENDING_REVIEW",
            amount_minor: body.amount_minor,
            currency: "INR",
            from_account_id: "acc-alice-01",
            to_account_id: null,
            payee_id: "payee-sim-01",
            reference: "Simulated attack",
            created_at: "2026-11-01T12:00:00Z",
            posted_at: null,
            step_up_required: false,
            message: "Transfer held for review",
          }),
        );
      }

      if (url.includes("/api/v1/fraud/alerts") && !url.includes("/al-lab-1")) {
        return Promise.resolve(json(200, [summary]));
      }

      if (url.includes("/api/v1/fraud/alerts/al-lab-1")) {
        return Promise.resolve(json(200, detail));
      }

      return Promise.resolve(json(404, { code: "NOT_FOUND", title: "not found" }));
    });
  });

  afterEach(() => {
    process.env.APP_ENV = originalAppEnv;
    vi.unstubAllGlobals();
  });

  it("1. renders UI cards for each scenario: Account Takeover, APP Scam, Mule Pass-Through, Velocity Burst, Impossible Travel, Amount Anomaly", () => {
    renderLab();

    expect(screen.getByRole("heading", { name: "Fraud attack simulator" })).toBeInTheDocument();

    // Verify all 4 required scenarios from acceptance criteria are explicitly present:
    expect(screen.getByTestId("scenario-card-F1")).toBeInTheDocument();
    expect(screen.getByTestId("scenario-card-F2")).toBeInTheDocument();
    expect(screen.getByTestId("scenario-card-F3")).toBeInTheDocument();
    expect(screen.getByTestId("scenario-card-F6")).toBeInTheDocument();

    // And verify F4 and F5 complete the F1-F6 suite:
    expect(screen.getByTestId("scenario-card-F4")).toBeInTheDocument();
    expect(screen.getByTestId("scenario-card-F5")).toBeInTheDocument();

    // Check titles
    expect(screen.getByRole("heading", { name: "APP Scam" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Velocity Burst" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Account Takeover" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Impossible Travel" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Amount Anomaly" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Mule Pass-Through" })).toBeInTheDocument();

    // Verify each card has an Execute Attack button
    const attackButtons = screen.getAllByRole("button", { name: /Execute Attack/i });
    expect(attackButtons).toHaveLength(6);
  });

  it("2. clicking 'Execute Attack' for F1 fires realistic sequence and displays live visual feedback (transfer held, alert raised, reasons)", async () => {
    const user = userEvent.setup();
    renderLab();

    const f1Card = screen.getByTestId("scenario-card-F1");
    const executeBtn = within(f1Card).getByRole("button", { name: /Execute Attack/i });

    await user.click(executeBtn);

    // Verify execution steps appear in aria-live container
    await waitFor(() => {
      expect(screen.getByText(/Authenticating target customer session/i)).toBeInTheDocument();
    });

    // Verify outcomes rendered
    const outcomeCard = await screen.findByTestId("attack-outcome-card");
    expect(outcomeCard).toBeInTheDocument();

    // Live visual feedback: transfer held
    expect(within(outcomeCard).getByText("Under review")).toBeInTheDocument();
    expect(within(outcomeCard).getByText("INR 30,000.00")).toBeInTheDocument();
    expect(within(outcomeCard).getByText("tx-lab-1")).toBeInTheDocument();

    // Live visual feedback: alert raised in staff queue with direct link
    const alertLink = within(outcomeCard).getByRole("link", { name: /View alert in staff queue/i });
    expect(alertLink).toBeInTheDocument();
    expect(alertLink).toHaveAttribute("href", "/staff/fraud/al-lab-1");

    // Live visual feedback: risk score and decision
    expect(within(outcomeCard).getByText("Risk score: 87/100")).toBeInTheDocument();
    expect(within(outcomeCard).getByText("Decision: Held for review")).toBeInTheDocument();

    // Live visual feedback: reasons generated
    expect(within(outcomeCard).getByText("Bank rule")).toBeInTheDocument();
    expect(
      within(outcomeCard).getByText(/Payee added 3m ago and INR 30,000.00 is large/i),
    ).toBeInTheDocument();
    expect(within(outcomeCard).getByText("+45 points")).toBeInTheDocument();

    expect(within(outcomeCard).getByText("Model")).toBeInTheDocument();
    expect(
      within(outcomeCard).getByText(/Payee was added 3 minutes before the transfer/i),
    ).toBeInTheDocument();
    expect(within(outcomeCard).getByText("+2.40 (SHAP)")).toBeInTheDocument();

    // Verify actual sequence of API calls
    const calledPaths = fetchMock.mock.calls.map(([req]) => urlOf(req));
    expect(calledPaths.some((p) => p.endsWith("/api/v1/auth/login"))).toBe(true);
    expect(calledPaths.some((p) => p.endsWith("/api/v1/accounts"))).toBe(true);
    expect(calledPaths.some((p) => p.endsWith("/api/v1/payees"))).toBe(true);
    expect(calledPaths.some((p) => p.includes("/api/v1/payees/") && p.endsWith("/verify"))).toBe(
      true,
    );
    expect(calledPaths.some((p) => p.endsWith("/api/v1/transfers"))).toBe(true);
  });

  it("3. clicking 'Execute Attack' for F2 (Velocity Burst) fires rapid burst of transfers", async () => {
    const user = userEvent.setup();
    renderLab();

    const f2Card = screen.getByTestId("scenario-card-F2");
    const executeBtn = within(f2Card).getByRole("button", { name: /Execute Attack/i });

    await user.click(executeBtn);

    await waitFor(() => {
      expect(
        screen.getByText(/Dispatching rapid burst of sequential transfers/i),
      ).toBeInTheDocument();
    });

    const outcomeCard = await screen.findByTestId("attack-outcome-card");
    expect(outcomeCard).toBeInTheDocument();

    // Check multiple transfer calls were made
    const transferCalls = fetchMock.mock.calls.filter(([req]) =>
      urlOf(req).endsWith("/api/v1/transfers"),
    );
    expect(transferCalls.length).toBeGreaterThanOrEqual(4);
  });

  it("4. clicking 'Execute Attack' for F3 (Account Takeover) simulates failed logins and new device ID", async () => {
    const user = userEvent.setup();
    renderLab();

    const f3Card = screen.getByTestId("scenario-card-F3");
    const executeBtn = within(f3Card).getByRole("button", { name: /Execute Attack/i });

    await user.click(executeBtn);

    await waitFor(() => {
      expect(screen.getByText(/Simulating credential brute-force/i)).toBeInTheDocument();
    });

    const outcomeCard = await screen.findByTestId("attack-outcome-card");
    expect(outcomeCard).toBeInTheDocument();

    // Verify failed login attempts were sent
    const loginCalls = fetchMock.mock.calls.filter(([req]) =>
      urlOf(req).endsWith("/api/v1/auth/login"),
    );
    expect(loginCalls.length).toBeGreaterThanOrEqual(4); // 3 failed + 1 successful
  });

  it("5. handles attack execution errors cleanly and surfaces the error alert", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(json(500, { code: "SERVER_ERROR", title: "DB connection down" })),
    );

    renderLab();

    const f1Card = screen.getByTestId("scenario-card-F1");
    const executeBtn = within(f1Card).getByRole("button", { name: /Execute Attack/i });

    await user.click(executeBtn);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Failed to authenticate target customer \(500\)/i,
    );
  });

  it("6. clearly isolated with demo-only guardrail disabled in production environments (APP_ENV=production)", () => {
    process.env.APP_ENV = "production";

    renderLab();

    // Production warning alert is displayed
    expect(
      screen.getByText(
        /Interactive attack simulation is disabled in production environments \(APP_ENV=production\)/i,
      ),
    ).toBeInTheDocument();

    // All Execute Attack buttons are disabled in production
    const buttons = screen.getAllByRole("button", { name: /Execute Attack/i });
    for (const btn of buttons) {
      expect(btn).toBeDisabled();
    }
  });

  it("7. enforces role authorization: non-analyst cannot access fraud lab", () => {
    renderLab(customerOnly);

    expect(
      screen.getByText(
        /Access restricted: the attack simulator requires the Fraud Analyst or Administrator role/i,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("scenario-card-F1")).not.toBeInTheDocument();
  });

  describe("fraudLabConfig security utilities", () => {
    it("generateIdempotencyKey produces distinct valid keys matching API constraints", () => {
      const regex = /^[A-Za-z0-9._:-]+$/;
      const seen = new Set<string>();

      for (let i = 0; i < 100; i++) {
        const key = generateIdempotencyKey("test");
        expect(key).toMatch(regex);
        expect(key.length).toBeGreaterThanOrEqual(8);
        expect(key.length).toBeLessThanOrEqual(128);
        expect(seen.has(key)).toBe(false);
        seen.add(key);
      }
    });

    it("secureRandom4Digits returns a 4-digit numeric string in range [1000, 9999]", () => {
      for (let i = 0; i < 100; i++) {
        const digits = secureRandom4Digits();
        expect(digits).toMatch(/^\d{4}$/);
        const num = Number.parseInt(digits, 10);
        expect(num).toBeGreaterThanOrEqual(1000);
        expect(num).toBeLessThanOrEqual(9999);
      }
    });
  });
});
