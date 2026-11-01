import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AlertDetail, AlertSummary, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, noContent, urlOf } from "../../test-utils";
import { FraudAlert } from "./FraudAlert";
import { FraudQueue } from "./FraudQueue";
import { barWidths, parseReasons, plainMessage } from "./fraudFormat";

const analyst: UserOut = {
  id: "u-fa",
  email: "analyst@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};
const compliance: UserOut = { ...analyst, id: "u-co", roles: ["COMPLIANCE_OFFICER"] };

const summary: AlertSummary = {
  id: "al-1",
  status: "OPEN",
  severity: "MEDIUM",
  risk_score: 72,
  decision: "REVIEW",
  amount_minor: 3_000_000,
  currency: "INR",
  transaction_status: "PENDING_REVIEW",
  created_at: "2026-11-01T10:00:00Z",
};

const detail: AlertDetail = {
  ...summary,
  transaction_id: "tx-1",
  customer_id: "cu-1",
  customer: {
    full_name: "Asha Rao",
    kyc_status: "VERIFIED",
    segment: "salaried",
    risk_profile: null,
    customer_since: "2025-03-01T00:00:00Z",
  },
  transaction_type: "TRANSFER",
  reference: "rent",
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
      code: "MODEL_RATIO_TO_AVG_AMOUNT",
      message: "Model: amount is 9.0x the customer's 30-day average",
      source: "MODEL",
      contribution: 1.2,
    },
    {
      code: "ANOMALY",
      message: "Unusual: ratio_to_avg_amount is 9.00, usually about 1.00",
      source: "ANOMALY",
      contribution: 0.8,
    },
  ],
  features: { ratio_to_avg_amount: 9 },
  model_version: "hybrid-lgbm-iforest-1",
  latency_ms: 21.4,
  resolution_note: null,
  resolved_at: null,
};

function renderAt(path: string, ui: ReactElement, me: UserOut = analyst) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/staff/fraud" element={ui} />
            <Route path="/staff/fraud/:alertId" element={ui} />
          </Routes>
        </MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reason formatting", () => {
  it("drops malformed reasons, strips prefixes and scales bars per source", () => {
    const reasons = parseReasons([
      ...detail.reasons,
      { code: 1 },
      { code: "X", message: "m", source: "EVIL" },
    ]);
    expect(reasons).toHaveLength(4);
    const model = reasons.filter((r) => r.source === "MODEL");
    const second = model[1];
    if (!second) throw new Error("expected two model reasons");
    expect(plainMessage(second)).toBe("Amount is 9.0x the customer's 30-day average");
    const widths = barWidths(reasons);
    expect(model.map((r) => widths.get(r))).toEqual([100, 50]);
  });
});

describe("FraudQueue", () => {
  it("lists open alerts and switches filters through the API", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input) =>
        Promise.resolve(json(200, urlOf(input).includes("status=OPEN") ? [summary] : [])),
      );
    renderAt("/staff/fraud", <FraudQueue />);
    const row = (await screen.findByRole("link", { name: /2026/ })).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("72")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("Held for review")).toBeInTheDocument();
    expect(urlOf(fetchMock.mock.calls[0]?.[0] ?? "")).toContain(
      "/api/v1/fraud/alerts?limit=100&status=OPEN",
    );
    await userEvent.click(screen.getByRole("button", { name: "Confirmed fraud" }));
    expect(await screen.findByText("No alerts.")).toBeInTheDocument();
    expect(urlOf(fetchMock.mock.calls.at(-1)?.[0] ?? "")).toContain("status=CONFIRMED_FRAUD");
  });

  it("shows an error with retry", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(500, { title: "boom" }));
    renderAt("/staff/fraud", <FraudQueue />);
    expect(await screen.findByRole("button", { name: /try again|retry/i })).toBeInTheDocument();
  });

  it("does not call the API for staff without the analyst role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderAt("/staff/fraud", <FraudQueue />, compliance);
    expect(screen.getByText(/need the Fraud Analyst or Administrator role/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("FraudAlert", () => {
  it("explains the score in plain language with SHAP bars and the customer profile", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, detail));
    renderAt("/staff/fraud/al-1", <FraudAlert />);
    expect(
      await screen.findByRole("heading", { name: "Risk 72: Held for review" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Payee was added 3 minutes before the transfer")).toBeInTheDocument();
    expect(screen.getByLabelText(/Payee was added 3 minutes.*\+2\.40 \(SHAP\)/)).toHaveAttribute(
      "value",
      "100",
    );
    expect(screen.getByLabelText(/Amount is 9\.0x.*\+1\.20 \(SHAP\)/)).toHaveAttribute(
      "value",
      "50",
    );
    expect(screen.getByText("+45 points")).toBeInTheDocument();
    expect(screen.getByText("80% unusual")).toBeInTheDocument();
    expect(
      screen.getByText("Amount compared with the 30-day average is 9.00, usually about 1.00"),
    ).toBeInTheDocument();
    expect(screen.getByText("Asha Rao")).toBeInTheDocument();
    expect(screen.getByLabelText("Risk score 72 of 100")).toHaveAttribute("value", "72");
  });

  it.each([
    ["Release", "RELEASE"],
    ["Reject", "REJECT"],
  ])("%s asks for confirmation and posts the decision with the note", async (label, action) => {
    let decided: unknown = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (urlOf(input).endsWith("/decision")) {
        decided = typeof init?.body === "string" ? JSON.parse(init.body) : null;
        return Promise.resolve(noContent());
      }
      return Promise.resolve(json(200, detail));
    });
    renderAt("/staff/fraud/al-1", <FraudAlert />);
    await userEvent.type(await screen.findByLabelText(/Note/), "called the customer");
    await userEvent.click(screen.getByRole("button", { name: label }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: label }));
    await waitFor(() => {
      expect(decided).toEqual({ action, note: "called the customer" });
    });
    expect(await screen.findByText("Decision recorded.")).toBeInTheDocument();
  });

  it("keeps the dialog open with the server's error when the decision fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        urlOf(input).endsWith("/decision")
          ? json(409, { title: "alert already resolved", code: "conflict", status: 409 }) // as core/errors.py sends it
          : json(200, detail),
      ),
    );
    renderAt("/staff/fraud/al-1", <FraudAlert />);
    await userEvent.click(await screen.findByRole("button", { name: "Release" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Release" }));
    expect(await within(dialog).findByText(/already resolved/)).toBeInTheDocument();
  });

  it("shows the resolution instead of actions for a resolved alert", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      json(200, {
        ...detail,
        status: "FALSE_POSITIVE",
        resolution_note: "known landlord",
        resolved_at: "2026-11-01T11:00:00Z",
      }),
    );
    renderAt("/staff/fraud/al-1", <FraudAlert />);
    expect(await screen.findByText(/known landlord/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Release" })).not.toBeInTheDocument();
  });
});
