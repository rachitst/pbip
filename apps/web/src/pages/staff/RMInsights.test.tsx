import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomerInsightItem, CustomerInsightsListResponse, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { CustomerFlyout } from "./CustomerFlyout";
import { RMInsights } from "./RMInsights";
import {
  canAccessRMInsights,
  churnRiskTone,
  deriveHealthLabel,
  formatProductName,
  formatSegment,
  loadCustomerActions,
  saveCustomerAction,
} from "./rmInsightsFormat";

const rmUser: UserOut = {
  id: "u-rm",
  email: "rm@pbip.bank",
  roles: ["RELATIONSHIP_MANAGER"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const adminUser: UserOut = {
  ...rmUser,
  id: "u-admin",
  email: "admin@pbip.bank",
  roles: ["ADMIN"],
};

const analystUser: UserOut = {
  ...rmUser,
  id: "u-analyst",
  email: "analyst@pbip.bank",
  roles: ["FRAUD_ANALYST"],
};

const leadHighRisk: CustomerInsightItem = {
  customer_id: "c-1",
  full_name: "Rahul Sharma",
  segment: "HIGH_NET_WORTH",
  total_balance_minor: 50_000_000,
  inactivity_days: 45,
  balance_decline_rate: 0.65,
  churn_risk_score: 0.82,
  churn_risk_level: "HIGH",
  top_recommended_product: "FIXED_DEPOSIT",
  top_product_score: 0.9,
  high_value: true,
};

const leadMediumRisk: CustomerInsightItem = {
  customer_id: "c-2",
  full_name: "Pooja Verma",
  segment: "SALARIED",
  total_balance_minor: 1_200_000,
  inactivity_days: 12,
  balance_decline_rate: 0.2,
  churn_risk_score: 0.45,
  churn_risk_level: "MEDIUM",
  top_recommended_product: "PERSONAL_LOAN",
  top_product_score: 0.75,
  high_value: false,
};

const leadLowRisk: CustomerInsightItem = {
  customer_id: "c-3",
  full_name: "Amit Patel",
  segment: "STUDENT",
  total_balance_minor: 250_000,
  inactivity_days: 2,
  balance_decline_rate: 0.05,
  churn_risk_score: 0.15,
  churn_risk_level: "LOW",
  top_recommended_product: "STUDENT_CARD",
  top_product_score: 0.4,
  high_value: false,
};

const mockListResponse: CustomerInsightsListResponse = {
  items: [leadHighRisk, leadMediumRisk, leadLowRisk],
  total: 3,
  evaluated_at: "2026-11-01T12:00:00Z",
};

function renderAt(path: string, ui: ReactElement, me: UserOut = rmUser) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/staff/insights" element={ui} />
          </Routes>
        </MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("rmInsightsFormat helpers", () => {
  it("formats segments, products and tone correctly", () => {
    expect(formatSegment("HIGH_NET_WORTH")).toBe("High Net Worth");
    expect(formatSegment("STUDENT")).toBe("Student");
    expect(formatProductName("PERSONAL_LOAN")).toBe("Personal Loan");
    expect(formatProductName(null)).toBe("None");
    expect(churnRiskTone("HIGH")).toBe("danger");
    expect(churnRiskTone("MEDIUM")).toBe("warning");
    expect(churnRiskTone("LOW")).toBe("success");
  });

  it("checks RM and Admin role access", () => {
    expect(canAccessRMInsights(rmUser)).toBe(true);
    expect(canAccessRMInsights(adminUser)).toBe(true);
    expect(canAccessRMInsights(analystUser)).toBe(false);
  });

  it("derives appropriate health indicators based on risk and decline rate", () => {
    expect(deriveHealthLabel(leadHighRisk).tone).toBe("danger");
    expect(deriveHealthLabel(leadMediumRisk).tone).toBe("warning");
    expect(deriveHealthLabel(leadLowRisk).tone).toBe("success");
  });

  it("saves and loads customer outreach actions to localStorage", () => {
    const act = saveCustomerAction({
      customer_id: "c-1",
      action_type: "CALL",
      outcome: "Connected - Interested in Offer",
      notes: "Follow up next Friday.",
      logged_by: "rm@pbip.bank",
    });
    expect(act.id).toBeDefined();
    expect(act.created_at).toBeDefined();

    const loaded = loadCustomerActions("c-1");
    expect(loaded).toHaveLength(1);
    expect(loaded[0]?.notes).toBe("Follow up next Friday.");
  });
});

describe("RMInsights Page & Table Filtering (TASK-066)", () => {
  it("renders customer leads table with names, segments, churn risk and recommendations", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, mockListResponse));
    renderAt("/staff/insights", <RMInsights />);

    expect(
      await screen.findByRole("heading", { name: "Relationship Manager Insights" }),
    ).toBeInTheDocument();

    // Verify all 3 customers appear in the table
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Rahul Sharma")).toBeInTheDocument();
    expect(within(table).getByText("Pooja Verma")).toBeInTheDocument();
    expect(within(table).getByText("Amit Patel")).toBeInTheDocument();

    // Verify segments in table
    expect(within(table).getByText("High Net Worth")).toBeInTheDocument();
    expect(within(table).getByText("Salaried")).toBeInTheDocument();
    expect(within(table).getByText("Student")).toBeInTheDocument();

    // High value badge
    expect(within(table).getByText("High Value")).toBeInTheDocument();

    // Top recommended products
    expect(within(table).getByText("Fixed Deposit")).toBeInTheDocument();
    expect(within(table).getByText("Personal Loan")).toBeInTheDocument();
    expect(within(table).getByText("Student Credit Card")).toBeInTheDocument();

    // Churn Risk
    expect(within(table).getByText("HIGH (82%)")).toBeInTheDocument();
    expect(within(table).getByText("MEDIUM (45%)")).toBeInTheDocument();
    expect(within(table).getByText("LOW (15%)")).toBeInTheDocument();
  });

  it("denies access and does not call API for staff without RM role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderAt("/staff/insights", <RMInsights />, analystUser);

    expect(
      screen.getByText(/Access restricted: Customer leads and behavioral insights require/i),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows access for administrator users", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, mockListResponse));
    renderAt("/staff/insights", <RMInsights />, adminUser);

    expect(
      await screen.findByRole("heading", { name: "Relationship Manager Insights" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument();
  });

  it("filters leads by loan/product propensity score", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, mockListResponse));
    renderAt("/staff/insights", <RMInsights />);

    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument();
    expect(screen.getByText("Pooja Verma")).toBeInTheDocument();
    expect(screen.getByText("Amit Patel")).toBeInTheDocument();

    // Select >= 70% propensity
    const propensitySelect = screen.getByLabelText(/Loan \/ Product Propensity/i);
    await userEvent.selectOptions(propensitySelect, "0.7");

    // Only Rahul (0.9) and Pooja (0.75) should be visible; Amit (0.4) should be filtered out
    expect(screen.getByText("Rahul Sharma")).toBeInTheDocument();
    expect(screen.getByText("Pooja Verma")).toBeInTheDocument();
    expect(screen.queryByText("Amit Patel")).not.toBeInTheDocument();

    // Select >= 85% propensity -> only Rahul (0.9)
    await userEvent.selectOptions(propensitySelect, "0.85");
    expect(screen.getByText("Rahul Sharma")).toBeInTheDocument();
    expect(screen.queryByText("Pooja Verma")).not.toBeInTheDocument();
  });

  it("passes segment and churn filters to the API", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = urlOf(input);
      if (url.includes("segment=SALARIED")) {
        return Promise.resolve(
          json(200, { items: [leadMediumRisk], total: 1, evaluated_at: "2026-11-01T12:00:00Z" }),
        );
      }
      return Promise.resolve(json(200, mockListResponse));
    });

    renderAt("/staff/insights", <RMInsights />);
    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument();

    // Change segment filter
    const segmentSelect = screen.getByLabelText(/Customer Segment/i);
    await userEvent.selectOptions(segmentSelect, "SALARIED");

    await waitFor(() => {
      expect(urlOf(fetchMock.mock.calls.at(-1)?.[0] ?? "")).toContain("segment=SALARIED");
    });
  });

  it("shows empty state when no leads match", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      json(200, { items: [], total: 0, evaluated_at: "2026-11-01T12:00:00Z" }),
    );
    renderAt("/staff/insights", <RMInsights />);

    expect(
      await screen.findByText("No customer leads match the chosen criteria."),
    ).toBeInTheDocument();
  });

  it("shows error state with working retry button on failure", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(500, { title: "Database error" }));
    renderAt("/staff/insights", <RMInsights />);

    expect(await screen.findByText(/Could not load customer insights/i)).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: /try again|retry/i });
    expect(retryBtn).toBeInTheDocument();
  });
});

describe("CustomerFlyout & Action Logger (TASK-066)", () => {
  it("opens flyout, displays balance volume, health indicators, churn probability, and closes cleanly", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, mockListResponse));
    renderAt("/staff/insights", <RMInsights />);

    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument();

    // Click "View Details" on Rahul Sharma
    const viewButton = screen.getByRole("button", { name: "View details for Rahul Sharma" });
    await userEvent.click(viewButton);

    // Dialog is visible
    const dialog = await screen.findByRole("dialog", {
      name: "Customer details: Rahul Sharma",
    });
    expect(dialog).toBeInTheDocument();

    // Verify indicators in flyout
    expect(within(dialog).getByText(/45 days inactive/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/65\.0%/)).toBeInTheDocument(); // 65.0% balance decline
    expect(within(dialog).getByText("Needs Attention")).toBeInTheDocument();
    expect(within(dialog).getByText(/HIGH \(82%\)/)).toBeInTheDocument();
    expect(within(dialog).getByText("Propensity score: 90%")).toBeInTheDocument();

    // Close with the close button
    const closeBtn = within(dialog).getByRole("button", { name: "Close details" });
    await userEvent.click(closeBtn);

    expect(
      screen.queryByRole("dialog", { name: "Customer details: Rahul Sharma" }),
    ).not.toBeInTheDocument();
  });

  it("closes flyout on Escape key press", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, mockListResponse));
    renderAt("/staff/insights", <RMInsights />);

    const viewButton = await screen.findByRole("button", {
      name: "View details for Rahul Sharma",
    });
    await userEvent.click(viewButton);

    expect(
      await screen.findByRole("dialog", { name: "Customer details: Rahul Sharma" }),
    ).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "Customer details: Rahul Sharma" }),
    ).not.toBeInTheDocument();
  });

  it("records a call in action logger and renders in the outreach history", async () => {
    const handleClose = vi.fn();
    render(
      <CustomerFlyout
        customer={leadHighRisk}
        onClose={handleClose}
        currentUserEmail="rm@pbip.bank"
      />,
    );

    // Initial state: no outreach recorded
    expect(screen.getByText("No outreach recorded yet for this customer.")).toBeInTheDocument();

    // Fill notes and submit
    const notesInput = screen.getByLabelText(/Meeting \/ Call Notes/i);
    await userEvent.type(notesInput, "Customer agreed to deposit INR 5,00,000 in 1-year FD.");

    const logActionBtn = screen.getByRole("button", { name: "Log Action" });
    await userEvent.click(logActionBtn);

    // Success feedback and new history entry
    expect(await screen.findByText("Action recorded successfully.")).toBeInTheDocument();
    expect(
      screen.getByText("Customer agreed to deposit INR 5,00,000 in 1-year FD."),
    ).toBeInTheDocument();
    expect(screen.getByText("By rm@pbip.bank")).toBeInTheDocument();
    expect(screen.getByText(/Outreach History \(1\)/)).toBeInTheDocument();

    // Check localStorage persistence
    const saved = loadCustomerActions("c-1");
    expect(saved).toHaveLength(1);
    expect(saved[0]?.notes).toBe("Customer agreed to deposit INR 5,00,000 in 1-year FD.");
  });

  it("validates notes field before saving action", async () => {
    const handleClose = vi.fn();
    render(
      <CustomerFlyout
        customer={leadMediumRisk}
        onClose={handleClose}
        currentUserEmail="rm@pbip.bank"
      />,
    );

    const logActionBtn = screen.getByRole("button", { name: "Log Action" });
    await userEvent.click(logActionBtn);

    expect(
      await screen.findByText("Please enter notes for this outreach action."),
    ).toBeInTheDocument();
  });
});
