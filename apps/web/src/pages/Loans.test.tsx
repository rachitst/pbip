import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AccountOut,
  EligibilityOut,
  EmiOut,
  LoanOut,
  LoanProductOut,
  LoanSummaryOut,
} from "../api/client";
import { json, urlOf } from "../test-utils";
import { clickstream } from "../telemetry/tracker";
import { Loans, formatRate } from "./Loans";

const account: AccountOut = {
  id: "a-1",
  account_number: "100000005678",
  account_number_masked: "XXXXXXXX5678",
  account_type: "SAVINGS",
  available_minor: 10_000_000,
  currency: "INR",
  held_minor: 0,
  ledger_balance_minor: 10_000_000,
  status: "ACTIVE",
};
const personal: LoanProductOut = {
  id: "p-1",
  loan_type: "PERSONAL",
  name: "Personal loan",
  annual_rate_bps: 1150,
  min_amount_minor: 2_500_000,
  max_amount_minor: 150_000_000,
  tenures_months: [12, 24, 36],
};
const summary: LoanSummaryOut = {
  id: "loan-1",
  product_id: "p-1",
  product_name: "Personal loan",
  loan_type: "PERSONAL",
  status: "ACTIVE",
  account_id: "a-1",
  currency: "INR",
  principal_minor: 10_000_000,
  annual_rate_bps: 1150,
  tenure_months: 12,
  emi_minor: 886_200,
  total_interest_minor: 634_400,
  total_payable_minor: 10_634_400,
  outstanding_principal_minor: 7_500_000,
  paid_instalments: 3,
  overdue_instalments: 1,
  next_due_date: "2027-02-02",
  next_due_amount_minor: 886_200,
  disbursed_at: "2026-11-02T10:00:00Z",
};
const detail: LoanOut = {
  ...summary,
  disbursal_transaction_id: "txn-disbursal-1",
  instalments: [
    {
      seq: 1,
      due_date: "2026-12-02",
      amount_minor: 886_200,
      principal_minor: 790_400,
      interest_minor: 95_800,
      balance_after_minor: 9_209_600,
      status: "PAID",
      paid_at: "2026-12-02T00:01:00Z",
    },
    {
      seq: 2,
      due_date: "2027-01-02",
      amount_minor: 886_200,
      principal_minor: 798_000,
      interest_minor: 88_200,
      balance_after_minor: 8_411_600,
      status: "OVERDUE",
      paid_at: null,
    },
    {
      seq: 3,
      due_date: "2027-02-02",
      amount_minor: 886_200,
      principal_minor: 805_600,
      interest_minor: 80_600,
      balance_after_minor: 7_606_000,
      status: "DUE",
      paid_at: null,
    },
  ],
};

function emiFor(principal: number, months: number): EmiOut {
  // A fixed fake figure per request, so the test can see WHICH request the page displayed.
  return {
    product_id: "p-1",
    annual_rate_bps: 1150,
    principal_minor: principal,
    tenure_months: months,
    emi_minor: (principal / 100 + months) * 100,
    total_interest_minor: 1_000,
    total_payable_minor: principal + 1_000,
    schedule: [],
  };
}

function eligibilityFor(amount: number, months: number, eligible = true): EligibilityOut {
  return {
    status: eligible ? "ELIGIBLE" : "NOT_ELIGIBLE",
    product_id: "p-1",
    requested_amount_minor: amount,
    tenure_months: months,
    monthly_income_minor: 10_000_000,
    existing_emi_minor: 0,
    proposed_emi_minor: 886_200,
    dti_existing: 0,
    dti_with_loan: eligible ? 0.09 : 0.71,
    max_eligible_amount_minor: 16_423_834,
    tenor_options: [],
    reasons: [
      eligible
        ? "An EMI of INR 8,862.00 brings repayments to 9% of your income, within the 50% limit."
        : "With an EMI of INR 1,30,000.00, repayments would take 71% of your income, above the 50% limit.",
    ],
  };
}

interface Options {
  loans?: () => Response;
  apply?: () => Response;
  products?: () => Response;
  eligible?: boolean;
}

type Body = Record<string, number | string>;

function bodyOf(init: RequestInit | undefined): Body {
  return JSON.parse(typeof init?.body === "string" ? init.body : "{}") as Body;
}

function mockApi(options: Options = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = urlOf(input);
    if (url === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
    if (url === "/api/v1/loans/products") {
      return Promise.resolve(options.products?.() ?? json(200, [personal]));
    }
    if (url === "/api/v1/loans") return Promise.resolve(options.loans?.() ?? json(200, []));
    if (url === "/api/v1/loans/loan-1") return Promise.resolve(json(200, detail));
    if (url === "/api/v1/loans/calculate-emi") {
      const body = bodyOf(init);
      return Promise.resolve(
        json(200, emiFor(Number(body.principal_minor), Number(body.tenure_months))),
      );
    }
    if (url === "/api/v1/loans/check-eligibility") {
      const body = bodyOf(init);
      return Promise.resolve(
        json(
          200,
          eligibilityFor(
            Number(body.amount_minor),
            Number(body.tenure_months),
            options.eligible ?? true,
          ),
        ),
      );
    }
    if (url === "/api/v1/loans/apply") {
      return Promise.resolve(options.apply?.() ?? json(201, detail));
    }
    return Promise.resolve(json(404, { title: "not mocked" }));
  });
}

function callsTo(fetchMock: ReturnType<typeof mockApi>, path: string) {
  return fetchMock.mock.calls.filter(([input]) => urlOf(input) === path);
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Loans />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Loans (TASK-051)", () => {
  it("formats basis points without floating point", () => {
    expect(formatRate(1150)).toBe("11.50% p.a.");
    expect(formatRate(925)).toBe("9.25% p.a.");
    expect(formatRate(850)).toBe("8.50% p.a.");
  });

  it("shows loading, then an empty state when there are no loans", async () => {
    mockApi();
    renderPage();
    expect(screen.getByText("Loading your loans...")).toBeInTheDocument();
    expect(await screen.findByText("You have no active loans.")).toBeInTheDocument();
  });

  it("shows an error state for the loans list and retries", async () => {
    let attempts = 0;
    mockApi({
      loans: () => {
        attempts += 1;
        return attempts === 1 ? json(500, { title: "x" }) : json(200, []);
      },
    });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/Could not load your loans/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("You have no active loans.")).toBeInTheDocument();
  });

  it("shows amortisation progress and the upcoming EMI dates of an active loan", async () => {
    mockApi({ loans: () => json(200, [summary]) });
    const user = userEvent.setup();
    renderPage();
    const card = await screen.findByRole("region", { name: "Personal loan" });
    const progress = within(card).getByRole("progressbar", {
      name: "Personal loan repayment progress",
    });
    expect(progress).toHaveAttribute("value", "3");
    expect(progress).toHaveAttribute("max", "12");
    expect(within(card).getByText(/3 of 12 EMIs paid/)).toBeInTheDocument();
    expect(within(card).getByText(/INR 75,000\.00/)).toBeInTheDocument(); // outstanding
    expect(within(card).getByText("One EMI is overdue.", { exact: false })).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Show upcoming EMIs" }));
    const table = await within(card).findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2); // the paid instalment is not "upcoming"
    expect(rows[0]).toHaveTextContent("2 of 12");
    expect(rows[0]).toHaveTextContent("Overdue");
    expect(rows[1]).toHaveTextContent("3 of 12");
    expect(rows[1]).toHaveTextContent("Upcoming");
  });

  it("debounces the sliders and shows the EMI the server computed in integer paise", async () => {
    const fetchMock = mockApi();
    const track = vi.spyOn(clickstream, "track");
    renderPage();
    const amount = await screen.findByLabelText("Loan amount");
    const estimate = () => screen.findByRole("region", { name: "EMI estimate" });
    await within(await estimate()).findByText("INR 25,012.00"); // initial: (25,00,000 paise / 100 + 12) rupees
    expect(track).not.toHaveBeenCalled(); // opening the page is not using the calculator
    fireEvent.change(amount, { target: { value: "3000000" } });
    fireEvent.change(amount, { target: { value: "3500000" } });
    fireEvent.change(screen.getByLabelText("Tenure"), { target: { value: "1" } });
    expect(screen.getByText("24 months")).toBeInTheDocument();
    await waitFor(async () => {
      expect(within(await estimate()).getByText("INR 35,024.00")).toBeInTheDocument();
    }); // (35,00,000 / 100 + 24) rupees
    const principals = callsTo(fetchMock, "/api/v1/loans/calculate-emi").map(
      ([, init]) => bodyOf(init).principal_minor,
    );
    expect(principals).toEqual([2_500_000, 3_500_000]); // no request for the 30,000 in between
    const last = callsTo(fetchMock, "/api/v1/loans/calculate-emi").at(-1);
    expect(bodyOf(last?.[1])).toEqual({
      product_id: "p-1",
      principal_minor: 3_500_000,
      tenure_months: 24,
    });
    // TASK-112: one event per settled change, with the loan type and never the amount
    expect(track.mock.calls).toEqual([["loan_calculator_used", { loan_type: "PERSONAL" }]]);
  });

  it("shows the eligibility pre-check with the maximum amount, the rate and the reasons", async () => {
    mockApi();
    renderPage();
    const card = await screen.findByRole("region", { name: "Eligibility pre-check" });
    expect(within(card).getByText("Eligible")).toBeInTheDocument();
    expect(within(card).getByText("INR 1,64,238.34")).toBeInTheDocument();
    expect(within(card).getByText("11.50% p.a.")).toBeInTheDocument();
    expect(within(card).getByText(/within the 50% limit/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply for this loan" })).toBeEnabled();
  });

  it("does not let a customer apply when the pre-check says not eligible", async () => {
    mockApi({ eligible: false });
    renderPage();
    const card = await screen.findByRole("region", { name: "Eligibility pre-check" });
    expect(within(card).getByText("Not eligible")).toBeInTheDocument();
    expect(within(card).getByText(/above the 50% limit/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply for this loan" })).toBeDisabled();
  });

  it("confirms, applies with one idempotency key and shows the disbursal receipt", async () => {
    let attempts = 0;
    const fetchMock = mockApi({
      apply: () => {
        attempts += 1;
        return attempts === 1 ? json(503, { title: "busy" }) : json(201, detail);
      },
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("region", { name: "Eligibility pre-check" });
    await user.click(screen.getByRole("button", { name: "Apply for this loan" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Personal loan")).toBeInTheDocument();
    expect(within(dialog).getByText("12 months")).toBeInTheDocument();
    expect(within(dialog).getByText("INR 25,000.00")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Confirm and receive the money" });
    await user.click(confirm);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("could not be completed");
    await user.click(confirm);
    const receipt = await screen.findByRole("region", { name: "Loan approved and disbursed" });
    expect(within(receipt).getByText("INR 1,00,000.00")).toBeInTheDocument();
    expect(within(receipt).getByText("txn-disbursal-1")).toBeInTheDocument();
    expect(within(receipt).getByText(/2 Dec 2026/)).toBeInTheDocument(); // first EMI due

    const applies = callsTo(fetchMock, "/api/v1/loans/apply");
    expect(applies).toHaveLength(2);
    const keys = applies.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[0]).toBe(keys[1]); // the retry of the same intent reuses the key
    expect(bodyOf(applies[0]?.[1])).toEqual({
      product_id: "p-1",
      amount_minor: 2_500_000,
      tenure_months: 12,
      account_id: "a-1",
    });
  });

  it("shows the server's plain reason when the application is refused", async () => {
    mockApi({
      apply: () =>
        json(422, {
          title: "This loan cannot be approved. Repayments would take 71% of your income.",
          code: "loan_not_eligible",
        }),
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("region", { name: "Eligibility pre-check" });
    await user.click(screen.getByRole("button", { name: "Apply for this loan" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Confirm and receive the money" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("71% of your income");
    expect(screen.queryByRole("region", { name: "Loan approved and disbursed" })).toBeNull();
  });

  it("shows an error state when the loan products cannot load", async () => {
    mockApi({ products: () => json(500, { title: "x" }) });
    renderPage();
    expect(
      await screen.findByText(/Could not load the loan products or your accounts/),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByLabelText("Loan amount")).toBeNull();
    });
  });
});
