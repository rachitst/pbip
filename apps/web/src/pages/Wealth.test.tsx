import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AccountOut,
  DepositQuoteOut,
  FixedDepositOut,
  PortfolioOut,
  WealthProductOut,
} from "../api/client";
import { json, urlOf } from "../test-utils";
import { Wealth, formatShareBps, maturityCountdown, tenureLabel } from "./Wealth";

const account: AccountOut = {
  id: "a-1",
  account_number: "100000005678",
  account_number_masked: "XXXXXXXX5678",
  account_type: "SAVINGS",
  available_minor: 20_000_000,
  currency: "INR",
  held_minor: 0,
  ledger_balance_minor: 20_000_000,
  status: "ACTIVE",
};
const regular: WealthProductOut = {
  id: "fd-regular",
  code: "FD-REGULAR",
  product_type: "FIXED_DEPOSIT",
  name: "Fixed deposit, 1 to 3 years",
  description: "Cumulative deposit: interest compounds quarterly and is paid with the principal.",
  risk_level: "LOW",
  yield_basis: "GUARANTEED",
  annual_rate_bps: 710,
  apy_bps: 729,
  tenures_months: [12, 18, 24, 36],
  min_lock_in_months: 0,
  min_amount_minor: 1_000_000,
  max_amount_minor: 1_000_000_000,
  premature_closure_allowed: true,
  premature_penalty_bps: 100,
  bookable: true,
  notes: "Indicative, simulated rate set by PBIP, not a real bank's offer.",
  catalogue_version: "2026-11-v1",
};
const index: WealthProductOut = {
  ...regular,
  id: "mf-index",
  code: "MF-INDEX",
  product_type: "MUTUAL_FUND",
  name: "Nifty 50 index fund (illustrative)",
  description: "Tracks the Nifty 50 index of large Indian companies.",
  risk_level: "HIGH",
  yield_basis: "MARKET_LINKED",
  annual_rate_bps: null,
  apy_bps: null,
  tenures_months: [],
  min_amount_minor: 50_000,
  max_amount_minor: null,
  premature_closure_allowed: false,
  premature_penalty_bps: null,
  bookable: false,
  notes: "Market-linked: returns are not guaranteed.",
};
const active: FixedDepositOut = {
  id: "dep-1",
  product_id: "fd-regular",
  product_name: "Fixed deposit, 1 to 3 years",
  status: "ACTIVE",
  account_id: "a-1",
  currency: "INR",
  principal_minor: 10_000_000,
  annual_rate_bps: 710,
  tenure_months: 12,
  start_date: "2026-11-02",
  maturity_date: "2027-11-02",
  days_to_maturity: 120,
  maturity_interest_minor: 729_128,
  maturity_amount_minor: 10_729_128,
  accrued_interest_minor: 412_345,
  lock_in_until: "2026-11-02",
  can_close_now: true,
  payout_if_closed_today_minor: 10_345_678,
  closure_kind: null,
  paid_interest_minor: null,
  closed_on: null,
  booking_transaction_id: "txn-book-1",
  closure_transaction_id: null,
  interest_transaction_id: null,
  booked_at: "2026-11-02T10:00:00Z",
};
const matured: FixedDepositOut = {
  ...active,
  id: "dep-2",
  status: "MATURED",
  days_to_maturity: 0,
  accrued_interest_minor: 729_128,
  payout_if_closed_today_minor: 10_729_128,
};
const taxSaver: FixedDepositOut = {
  ...active,
  id: "dep-3",
  product_name: "Tax-saver fixed deposit, 5 years",
  lock_in_until: "2031-11-02",
  can_close_now: false,
  payout_if_closed_today_minor: null,
};
const closed: FixedDepositOut = { ...active, id: "dep-4", status: "CLOSED" };
const portfolio: PortfolioOut = {
  currency: "INR",
  as_of: "2026-11-02",
  cash_minor: 10_000_000,
  invested_principal_minor: 10_000_000,
  accrued_interest_minor: 412_345,
  deposits_value_minor: 10_412_345,
  projected_maturity_value_minor: 10_729_128,
  funds_minor: 0,
  total_minor: 20_412_345,
  open_deposits: 1,
  allocation: [
    { asset_class: "CASH", value_minor: 10_000_000, share_bps: 4899 },
    { asset_class: "DEPOSITS", value_minor: 10_412_345, share_bps: 5101 },
    { asset_class: "FUNDS", value_minor: 0, share_bps: 0 },
  ],
};
const emptyPortfolio: PortfolioOut = {
  ...portfolio,
  cash_minor: 0,
  invested_principal_minor: 0,
  accrued_interest_minor: 0,
  deposits_value_minor: 0,
  projected_maturity_value_minor: 0,
  total_minor: 0,
  open_deposits: 0,
  allocation: portfolio.allocation.map((s) => ({ ...s, value_minor: 0, share_bps: 0 })),
};

function quoteFor(amount: number, months: number): DepositQuoteOut {
  // A fixed fake figure per request, so the test can see WHICH request the dialog displayed.
  return {
    product_id: "fd-regular",
    product_name: regular.name,
    principal_minor: amount,
    tenure_months: months,
    annual_rate_bps: 710,
    apy_bps: 729,
    start_date: "2026-11-02",
    maturity_date: months === 12 ? "2027-11-02" : "2028-05-02",
    maturity_interest_minor: amount / 100 + months,
    maturity_amount_minor: amount + amount / 100 + months,
    lock_in_months: 0,
    premature_penalty_bps: 100,
  };
}

interface Options {
  portfolio?: () => Response;
  deposits?: () => Response;
  products?: (url: string) => Response;
  book?: () => Response;
  close?: () => Response;
}

type Body = Record<string, number | string>;

function bodyOf(init: RequestInit | undefined): Body {
  return JSON.parse(typeof init?.body === "string" ? init.body : "{}") as Body;
}

function mockApi(options: Options = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = urlOf(input);
    if (url === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
    if (url === "/api/v1/wealth/portfolio") {
      return Promise.resolve(options.portfolio?.() ?? json(200, portfolio));
    }
    if (url === "/api/v1/wealth/fixed-deposits") {
      return Promise.resolve(options.deposits?.() ?? json(200, []));
    }
    if (url.startsWith("/api/v1/wealth/products")) {
      return Promise.resolve(options.products?.(url) ?? json(200, [regular, index]));
    }
    if (url === "/api/v1/wealth/fixed-deposits/quote") {
      const body = bodyOf(init);
      return Promise.resolve(
        json(200, quoteFor(Number(body.amount_minor), Number(body.tenure_months))),
      );
    }
    if (url === "/api/v1/wealth/fixed-deposits/book") {
      return Promise.resolve(options.book?.() ?? json(201, active));
    }
    if (url.endsWith("/close")) {
      return Promise.resolve(options.close?.() ?? json(200, { ...active, status: "CLOSED" }));
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
      <MemoryRouter>
        <Wealth />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Wealth (TASK-065)", () => {
  it("formats shares, countdowns and tenures without floating point", () => {
    expect(formatShareBps(729)).toBe("7.29%");
    expect(formatShareBps(10_000)).toBe("100.00%");
    expect(formatShareBps(5)).toBe("0.05%");
    expect(maturityCountdown({ status: "ACTIVE", days_to_maturity: 120 })).toBe("120 days to go");
    expect(maturityCountdown({ status: "ACTIVE", days_to_maturity: 1 })).toBe("1 day to go");
    expect(maturityCountdown({ status: "MATURED", days_to_maturity: 0 })).toBe(
      "Matured: ready to withdraw",
    );
    expect(tenureLabel(12)).toBe("1 year");
    expect(tenureLabel(36)).toBe("3 years");
    expect(tenureLabel(9)).toBe("9 months");
  });

  it("shows loading, then empty states with no deposits and nothing invested", async () => {
    mockApi({ portfolio: () => json(200, emptyPortfolio) });
    renderPage();
    expect(screen.getByText("Loading your portfolio...")).toBeInTheDocument();
    expect(screen.getByText("Loading your fixed deposits...")).toBeInTheDocument();
    expect(await screen.findByText("You have no active fixed deposits.")).toBeInTheDocument();
    expect(await screen.findByText(/Nothing to allocate yet/)).toBeInTheDocument();
  });

  it("shows an error state for the portfolio and retries", async () => {
    let attempts = 0;
    mockApi({
      portfolio: () => {
        attempts += 1;
        return attempts === 1 ? json(500, { title: "x" }) : json(200, portfolio);
      },
    });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/Could not load your portfolio/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "Portfolio summary" })).toBeInTheDocument();
  });

  it("shows invested capital, accrued returns and the Cash/Deposits/Funds allocation", async () => {
    mockApi();
    renderPage();
    const summary = await screen.findByRole("region", { name: "Portfolio summary" });
    const row = (label: string) => within(summary).getByText(label).closest("div");
    expect(row("Invested capital")).toHaveTextContent("INR 1,00,000.00");
    expect(row("Accrued returns")).toHaveTextContent("INR 4,123.45");
    expect(row("Value at maturity")).toHaveTextContent("INR 1,07,291.28");
    expect(row("Total")).toHaveTextContent("INR 2,04,123.45");
    expect(screen.getByRole("region", { name: "Asset allocation chart" })).toBeInTheDocument();
    const legend = screen.getByRole("list", { name: "Asset allocation" });
    const items = within(legend).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual([
      "CashINR 1,00,000.0048.99%",
      "Fixed depositsINR 1,04,123.4551.01%",
      "FundsINR 0.000.00%",
    ]);
  });

  it("lists active deposits with the maturity countdown and payout, hiding closed ones", async () => {
    mockApi({ deposits: () => json(200, [active, matured, taxSaver, closed]) });
    renderPage();
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("120 days to go");
    expect(rows[0]).toHaveTextContent("INR 1,07,291.28");
    expect(rows[0]).toHaveTextContent("incl. INR 7,291.28 interest");
    expect(rows[0]).toHaveTextContent("7.10% p.a.");
    expect(rows[0]).toHaveTextContent("INR 4,123.45"); // earned so far
    expect(
      within(rows[0] as HTMLElement).getByRole("button", { name: "Close early" }),
    ).toBeVisible();
    expect(rows[1]).toHaveTextContent("Matured: ready to withdraw");
    expect(within(rows[1] as HTMLElement).getByRole("button", { name: "Withdraw" })).toBeVisible();
    expect(rows[2]).toHaveTextContent("Locked until 2 Nov 2031");
    expect(screen.getByText(/A matured deposit earns no further interest/)).toBeInTheDocument();
  });

  it("closes a deposit early after review, with an idempotency key", async () => {
    const fetchMock = mockApi({ deposits: () => json(200, [active]) });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Close early" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("INR 1,03,456.78")).toBeInTheDocument(); // payout today
    await user.click(within(dialog).getByRole("button", { name: "Close early and withdraw" }));
    await waitFor(() => {
      expect(callsTo(fetchMock, "/api/v1/wealth/fixed-deposits/dep-1/close")).toHaveLength(1);
    });
    const [, init] = callsTo(fetchMock, "/api/v1/wealth/fixed-deposits/dep-1/close")[0] ?? [];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("shows the catalogue with rates, a market-linked fund without a yield, and filters by risk", async () => {
    const fetchMock = mockApi({
      products: (url) => json(200, url.includes("HIGH") ? [index] : [regular, index]),
    });
    const user = userEvent.setup();
    renderPage();
    const fd = await screen.findByRole("region", { name: regular.name });
    expect(within(fd).getByText("7.10% p.a.")).toBeInTheDocument();
    expect(within(fd).getByText("7.29%")).toBeInTheDocument(); // APY
    expect(within(fd).getByText("1 year, 18 months, 2 years, 3 years")).toBeInTheDocument();
    const fund = screen.getByRole("region", { name: index.name });
    expect(within(fund).getByText("Market-linked: no stated yield")).toBeInTheDocument();
    expect(within(fund).queryByRole("button", { name: "Book this deposit" })).toBeNull();
    await user.selectOptions(screen.getByLabelText("Risk level"), "HIGH");
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: regular.name })).toBeNull();
    });
    expect(callsTo(fetchMock, "/api/v1/wealth/products?risk_level=HIGH")).toHaveLength(1);
  });

  it("books a deposit with the exact maturity preview from the API and one idempotency key", async () => {
    let attempts = 0;
    const fetchMock = mockApi({
      book: () => {
        attempts += 1;
        return attempts === 1 ? json(503, { title: "busy" }) : json(201, active);
      },
    });
    const user = userEvent.setup();
    renderPage();
    const card = await screen.findByRole("region", { name: regular.name });
    await user.click(within(card).getByRole("button", { name: "Book this deposit" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Amount", { exact: false }), "100000");
    await user.selectOptions(within(dialog).getByLabelText("Tenure"), "18");
    const preview = await within(dialog).findByRole("region", { name: "Maturity preview" });
    // (1,00,00,000 paise / 100 + 18) paise of interest: the figure of the 18-month request
    expect(await within(preview).findByText("INR 1,000.18")).toBeInTheDocument();
    expect(within(preview).getByText("INR 1,01,000.18")).toBeInTheDocument();
    expect(within(preview).getByText("2 May 2028")).toBeInTheDocument();

    const confirm = within(dialog).getByRole("button", { name: "Confirm and book" });
    await user.click(confirm);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("could not be booked");
    await user.click(confirm);
    const receipt = await within(dialog).findByRole("region", { name: "Booking receipt" });
    expect(within(receipt).getByText("txn-book-1")).toBeInTheDocument();

    const books = callsTo(fetchMock, "/api/v1/wealth/fixed-deposits/book");
    expect(books).toHaveLength(2);
    const keys = books.map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[0]).toBe(keys[1]); // the retry of the same intent reuses the key
    expect(bodyOf(books[0]?.[1])).toEqual({
      product_id: "fd-regular",
      amount_minor: 10_000_000,
      tenure_months: 18,
      account_id: "a-1",
    });
  });

  it("shows the server's reason when the balance is too low and books nothing", async () => {
    mockApi({
      book: () => json(422, { title: "insufficient funds", code: "insufficient_funds" }),
    });
    const user = userEvent.setup();
    renderPage();
    const card = await screen.findByRole("region", { name: regular.name });
    await user.click(within(card).getByRole("button", { name: "Book this deposit" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Amount", { exact: false }), "50000");
    await within(dialog).findByRole("region", { name: "Maturity preview" });
    await user.click(within(dialog).getByRole("button", { name: "Confirm and book" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/insufficient funds/i);
    expect(within(dialog).queryByRole("region", { name: "Booking receipt" })).toBeNull();
  });

  it("refuses an amount outside the product's range before asking the server", async () => {
    const fetchMock = mockApi();
    const user = userEvent.setup();
    renderPage();
    const card = await screen.findByRole("region", { name: regular.name });
    await user.click(within(card).getByRole("button", { name: "Book this deposit" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Amount", { exact: false }), "500");
    expect(await within(dialog).findByText(/Enter an amount from INR 10,000.00/)).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Confirm and book" })).toBeDisabled();
    expect(callsTo(fetchMock, "/api/v1/wealth/fixed-deposits/quote")).toHaveLength(0);
  });

  it("shows an error state when the catalogue cannot load", async () => {
    mockApi({ products: () => json(500, { title: "x" }) });
    renderPage();
    expect(await screen.findByText(/Could not load the wealth products/)).toBeInTheDocument();
  });

  it("links to the side-by-side product comparison (TASK-098)", async () => {
    mockApi();
    renderPage();
    const link = await screen.findByRole("link", { name: "Compare products side by side" });
    expect(link).toHaveAttribute("href", "/wealth/compare");
  });
});
