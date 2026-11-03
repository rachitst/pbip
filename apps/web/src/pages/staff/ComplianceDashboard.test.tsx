import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComplianceExceptionsOut, ExceptionOut, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { ComplianceDashboard, Indicators, canManageCompliance } from "./ComplianceDashboard";

const officer: UserOut = {
  id: "u-co",
  email: "compliance@demo.pbip",
  roles: ["COMPLIANCE_OFFICER"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const EVENT = "e0e00000-1111-4222-8333-444455556666";
const LOCK = "10c00000-1111-4222-8333-444455556666";
const zero = { OPEN: 0, ACKNOWLEDGED: 0, RESOLVED: 0 };

const blocked: ExceptionOut = {
  id: `BLOCKED_TRANSFER:${EVENT}`,
  kind: "BLOCKED_TRANSFER",
  ref: EVENT,
  occurred_at: new Date(Date.now() - 60_000).toISOString(),
  status: "OPEN",
  acknowledged: null,
  resolved: null,
  risk_score: 95,
  amount_minor: 12_345,
  currency: "INR",
  transaction_type: "TRANSFER",
  reason_codes: ["NEW_PAYEE_LARGE"],
  first_invalid_seq: null,
};

const lockout: ExceptionOut = {
  ...blocked,
  id: `ACCOUNT_LOCKOUT:${LOCK}`,
  kind: "ACCOUNT_LOCKOUT",
  ref: LOCK,
  status: "ACKNOWLEDGED",
  acknowledged: {
    by_user_id: "u-co",
    at: new Date().toISOString(),
    reason: "Calling the customer",
  },
  risk_score: null,
  amount_minor: null,
  currency: null,
  transaction_type: null,
  reason_codes: [],
};

const empty: ComplianceExceptionsOut = {
  generated_at: new Date().toISOString(),
  indicators: {
    window_days: 30,
    review_sla_hours: 24,
    acknowledge_sla_hours: 24,
    unresolved_exceptions: 0,
    unacknowledged_past_sla: 0,
    reviews_pending: 0,
    reviews_past_sla: 0,
    oldest_review_at: null,
    by_kind: { BLOCKED_TRANSFER: zero, ACCOUNT_LOCKOUT: zero, AUDIT_CHAIN_FAILURE: zero },
  },
  audit_chain: { valid: true, checked: 120, first_invalid_seq: null },
  items: [],
};

const full: ComplianceExceptionsOut = {
  ...empty,
  indicators: {
    ...empty.indicators,
    unresolved_exceptions: 2,
    unacknowledged_past_sla: 1,
    reviews_pending: 4,
    reviews_past_sla: 2,
    oldest_review_at: new Date(Date.now() - 30 * 3600_000).toISOString(),
    by_kind: {
      BLOCKED_TRANSFER: { OPEN: 1, ACKNOWLEDGED: 0, RESOLVED: 3 },
      ACCOUNT_LOCKOUT: { OPEN: 0, ACKNOWLEDGED: 1, RESOLVED: 0 },
      AUDIT_CHAIN_FAILURE: zero,
    },
  },
  items: [blocked, lockout],
};

function renderWith(ui: ReactElement, me: UserOut = officer) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>{ui}</MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

function metric(name: string): HTMLElement {
  const term = screen.getByText(name, { selector: "dt" });
  const group = term.parentElement;
  if (!group) throw new Error("metric without a group");
  return group;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("canManageCompliance", () => {
  it("admits compliance officers and admins only", () => {
    expect(canManageCompliance(officer)).toBe(true);
    expect(canManageCompliance({ ...officer, roles: ["ADMIN"] })).toBe(true);
    expect(canManageCompliance({ ...officer, roles: ["FRAUD_ANALYST"] })).toBe(false);
    expect(canManageCompliance({ ...officer, roles: ["RELATIONSHIP_MANAGER"] })).toBe(false);
  });
});

describe("Regulatory indicators", () => {
  it("shows every indicator with its definition and the per-kind table", () => {
    renderWith(<Indicators data={full} />);
    expect(within(metric("Unresolved exceptions")).getByText("2")).toBeInTheDocument();
    expect(
      within(metric("Not acknowledged within SLA")).getByText(/more than 24 hours ago/),
    ).toBeInTheDocument();
    expect(within(metric("Fraud reviews past SLA")).getByText("2")).toBeInTheDocument();
    expect(within(metric("Fraud reviews pending")).getByText("4")).toBeInTheDocument();
    expect(within(metric("Audit chain")).getByText("Verified")).toBeInTheDocument();
    expect(within(metric("Audit chain")).getByText(/120 entries verified/)).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /Blocked payment/ });
    expect(
      within(row)
        .getAllByRole("cell")
        .map((c) => c.textContent),
    ).toEqual(["1", "0", "3"]);
  });

  it("names the per-kind table outside its scroll box so the text wraps at phone width", () => {
    // Regression (TASK-089 bug bash): a long centred <caption> is as wide as the table and was
    // cut off inside the horizontal scroll box at 360 px.
    renderWith(<Indicators data={full} />);
    const table = screen.getByRole("table", {
      name: "Exceptions by kind and status, last 30 days",
    });
    expect(table).toHaveAccessibleDescription(/acknowledged within 24 hours/);
    expect(table.querySelector("caption")).toBeNull();
    const note = screen.getByText(/Service levels are set by the bank/);
    expect(note.closest(".entries")).toBeNull();
  });

  it("flags a broken audit chain with the first failing entry", () => {
    renderWith(
      <Indicators
        data={{ ...empty, audit_chain: { valid: false, checked: 6, first_invalid_seq: 7 } }}
      />,
    );
    expect(within(metric("Audit chain")).getByText("Broken at #7")).toBeInTheDocument();
    expect(within(metric("Audit chain")).getByText(/before the break/)).toBeInTheDocument();
  });
});

describe("ComplianceDashboard page (TASK-100)", () => {
  it("loads exceptions with the chosen filters and shows actions by status", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, full));
    const user = userEvent.setup();
    renderWith(<ComplianceDashboard />);
    expect(screen.getByText("Loading compliance exceptions...")).toBeInTheDocument();
    const blockedRow = (await screen.findByText("risk 95 · NEW_PAYEE_LARGE")).closest("tr");
    if (!blockedRow) throw new Error("no row");
    expect(within(blockedRow).getByText("Open")).toBeInTheDocument();
    expect(within(blockedRow).getByRole("button", { name: "Acknowledge" })).toBeInTheDocument();
    const lockRow = screen.getByText(/Calling the customer/).closest("tr");
    if (!lockRow) throw new Error("no row");
    expect(within(lockRow).queryByRole("button", { name: "Acknowledge" })).toBeNull();
    expect(within(lockRow).getByRole("button", { name: "Resolve" })).toBeInTheDocument();
    expect(urlOf(fetchMock.mock.calls[0]?.[0] ?? "")).toContain("/compliance/exceptions?days=30");

    await user.selectOptions(screen.getByLabelText("Kind"), "ACCOUNT_LOCKOUT");
    await user.selectOptions(screen.getByLabelText("Period"), "90");
    await waitFor(() => {
      expect(urlOf(fetchMock.mock.calls.at(-1)?.[0] ?? "")).toContain(
        "days=90&kind=ACCOUNT_LOCKOUT",
      );
    });
  });

  it("acknowledges with a reason and refreshes the list", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((_input, init) =>
        Promise.resolve(
          init?.method === "POST"
            ? json(200, { ...blocked, status: "ACKNOWLEDGED" })
            : json(200, full),
        ),
      );
    const user = userEvent.setup();
    renderWith(<ComplianceDashboard />);
    const row = (await screen.findByText("risk 95 · NEW_PAYEE_LARGE")).closest("tr");
    if (!row) throw new Error("no row");
    await user.click(within(row).getByRole("button", { name: "Acknowledge" }));
    const dialog = screen.getByRole("dialog", { name: "Acknowledge blocked payment" });
    const submit = within(dialog).getByRole("button", { name: "Acknowledge" });
    expect(submit).toBeDisabled(); // a reason is required
    await user.type(within(dialog).getByLabelText(/Reason/), "  Checked with fraud team ");
    await user.click(submit);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(urlOf(post?.[0] ?? "")).toContain(
      `/compliance/exceptions/BLOCKED_TRANSFER/${EVENT}/acknowledge`,
    );
    expect(post?.[1]?.body).toBe(JSON.stringify({ reason: "Checked with fraud team" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([, i]) => i?.method !== "POST")).toHaveLength(2);
    });
  });

  it("shows the server's conflict message in the dialog", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) =>
      Promise.resolve(
        init?.method === "POST"
          ? json(409, { code: "conflict", title: "The exception is already resolved" })
          : json(200, full),
      ),
    );
    const user = userEvent.setup();
    renderWith(<ComplianceDashboard />);
    const row = (await screen.findByText(/Calling the customer/)).closest("tr");
    if (!row) throw new Error("no row");
    await user.click(within(row).getByRole("button", { name: "Resolve" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "done");
    await user.click(within(dialog).getByRole("button", { name: "Resolve" }));
    expect(await within(dialog).findByText(/already resolved/)).toBeInTheDocument();
  });

  it("has an empty state and an error state with retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(500, { title: "boom" }))
      .mockResolvedValue(json(200, empty));
    const user = userEvent.setup();
    renderWith(<ComplianceDashboard />);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No exceptions in the last 30 days.")).toBeInTheDocument();
  });

  it("does not call the API for staff without the compliance role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderWith(<ComplianceDashboard />, { ...officer, roles: ["RELATIONSHIP_MANAGER"] });
    expect(screen.getByText(/needs the Compliance Officer or Administrator role/)).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
