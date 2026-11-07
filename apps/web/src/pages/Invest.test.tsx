import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InstrumentOut, OrderOut, SipOut, SuitabilityOut } from "../api/client";
import { account, json, urlOf } from "../test-utils";
import Invest, { preselected } from "./Invest";

const indexFund: InstrumentOut = {
  id: "50000000-0000-4000-a000-000000000003",
  code: "MF-INDEX",
  kind: "MUTUAL_FUND",
  name: "Nifty 50 index fund (illustrative)",
  description: "Tracks the Nifty 50 index of large Indian companies.",
  risk_level: "HIGH",
  wealth_product_id: "20000000-0000-4000-a000-000000000006",
  currency: "INR",
  order_mode: "AMOUNT",
  min_order_minor: 500_00,
  price_minor: 300_00,
  price_date: "2026-11-05",
  price_source: "SIMULATED",
  notes: "Simulated NAV.",
  catalogue_version: "2026-11-v2",
};
const stock: InstrumentOut = {
  ...indexFund,
  id: "50000000-0000-4000-a000-000000000004",
  code: "STK-SIMINFRA",
  kind: "STOCK",
  name: "Simulated Infrastructure Ltd (fictional)",
  wealth_product_id: null,
  order_mode: "UNITS",
  min_order_minor: null,
  price_minor: 845_50,
};
const moderate: SuitabilityOut = {
  band: "MODERATE",
  questionnaire_version: "RTQ-1",
  assessed_at: "2026-11-02T09:30:00Z",
  items: [
    {
      product_id: indexFund.id,
      kind: "INSTRUMENT",
      risk_level: "HIGH",
      suitable: false,
      reason: "High risk is above your moderate risk band.",
    },
  ],
};
const order: OrderOut = {
  id: "order-1",
  side: "BUY",
  holding_id: "holding-1",
  instrument_id: indexFund.id,
  instrument_code: indexFund.code,
  instrument_name: indexFund.name,
  account_id: account.id,
  units_micro: 3_333_333,
  price_minor: 300_00,
  price_date: "2026-11-05",
  price_source: "SIMULATED",
  amount_minor: 1_000_00,
  cost_basis_minor: 1_000_00,
  realised_gain_minor: null,
  transaction_id: "txn-1",
  sip_id: null,
  created_at: "2026-11-05T06:00:00Z",
};

const sip: SipOut = {
  id: "sip-1",
  instrument_id: indexFund.id,
  instrument_code: indexFund.code,
  instrument_name: indexFund.name,
  account_id: account.id,
  amount_minor: 2_000_00,
  day_of_month: 10,
  status: "ACTIVE",
  next_run_on: "2026-11-10",
  created_at: "2026-11-05T06:00:00Z",
  cancelled_at: null,
  runs: [],
};

function mockApi() {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = urlOf(input);
    if (url === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
    if (url === "/api/v1/wealth/instruments") return Promise.resolve(json(200, [stock, indexFund]));
    if (url === "/api/v1/wealth/suitability") return Promise.resolve(json(200, moderate));
    if (url === "/api/v1/wealth/investments/buy") return Promise.resolve(json(201, order));
    if (url === "/api/v1/wealth/sips") return Promise.resolve(json(201, sip));
    return Promise.resolve(json(404, { title: "not mocked" }));
  });
}

function bodyOf(fetchMock: ReturnType<typeof mockApi>, path: string): unknown {
  const call = fetchMock.mock.calls.find(([input]) => urlOf(input) === path);
  const init = call?.[1];
  return JSON.parse(typeof init?.body === "string" ? init.body : "null");
}

function renderPage(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Invest />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Invest (ADR-0058)", () => {
  it("preselects by instrument id or by the fund's catalogue id", () => {
    expect(preselected([stock, indexFund], indexFund.wealth_product_id).id).toBe(indexFund.id);
    expect(preselected([stock, indexFund], stock.id).id).toBe(stock.id);
    expect(preselected([stock, indexFund], "unknown").id).toBe(stock.id);
  });

  it("buys a fund by amount after review, says the price is simulated, and warns on the band", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = mockApi();
    renderPage(`/wealth/invest?product=${indexFund.wealth_product_id ?? ""}`);
    const form = within(await screen.findByRole("region", { name: "Order" }));
    expect(form.getByLabelText("Investment")).toHaveValue(indexFund.id);
    expect(form.getByText(/not market data/)).toBeInTheDocument();
    expect(await form.findByText("High risk is above your moderate risk band.")).toBeVisible();
    await user.type(form.getByLabelText("Amount"), "1000");
    await user.click(form.getByRole("button", { name: "Review order" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Confirm your order" }));
    expect(dialog.getByText("3.333333")).toBeInTheDocument(); // rounded down like the server
    await user.click(dialog.getByRole("button", { name: "Buy now" }));
    const receipt = within(await screen.findByRole("region", { name: "Order receipt" }));
    expect(receipt.getByText("txn-1")).toBeInTheDocument();
    expect(bodyOf(fetchMock, "/api/v1/wealth/investments/buy")).toEqual({
      instrument_id: indexFund.id,
      account_id: account.id,
      amount_minor: 1_000_00,
    });
    const call = fetchMock.mock.calls.find(([i]) => urlOf(i) === "/api/v1/wealth/investments/buy");
    expect(new Headers(call?.[1]?.headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("buys whole units of a stock and refuses part of one", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = mockApi();
    renderPage(`/wealth/invest?product=${stock.id}`);
    const form = within(await screen.findByRole("region", { name: "Order" }));
    await user.type(form.getByLabelText("Units"), "2.5");
    expect(form.getByText("Enter a whole number of units.")).toBeInTheDocument();
    expect(form.getByRole("button", { name: "Review order" })).toBeDisabled();
    await user.clear(form.getByLabelText("Units"));
    await user.type(form.getByLabelText("Units"), "2");
    await user.click(form.getByRole("button", { name: "Review order" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Confirm your order" }));
    expect(dialog.getByText("INR 1,691.00")).toBeInTheDocument();
    await user.click(dialog.getByRole("button", { name: "Buy now" }));
    await screen.findByRole("region", { name: "Order receipt" });
    expect(bodyOf(fetchMock, "/api/v1/wealth/investments/buy")).toEqual({
      instrument_id: stock.id,
      account_id: account.id,
      units: 2,
    });
  });

  it("starts a monthly SIP for a fund after review, with one key, moving no money", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = mockApi();
    renderPage(`/wealth/invest?product=${indexFund.id}`);
    const form = within(await screen.findByRole("region", { name: "Start a monthly SIP" }));
    await user.type(form.getByLabelText("Monthly amount"), "2000");
    await user.selectOptions(form.getByLabelText("Day of the month"), "10");
    await user.click(form.getByRole("button", { name: "Review SIP" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Confirm your monthly SIP" }));
    expect(dialog.getByText("INR 2,000.00")).toBeInTheDocument();
    await user.click(dialog.getByRole("button", { name: "Start SIP" }));
    expect(await form.findByRole("status")).toHaveTextContent("nothing is debited today");
    expect(bodyOf(fetchMock, "/api/v1/wealth/sips")).toEqual({
      instrument_id: indexFund.id,
      account_id: account.id,
      amount_minor: 2_000_00,
      day_of_month: 10,
    });
    const call = fetchMock.mock.calls.find(([i]) => urlOf(i) === "/api/v1/wealth/sips");
    expect(new Headers(call?.[1]?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([i]) => urlOf(i).endsWith("/investments/buy"))).toBe(false);
  });

  it("offers no SIP for a stock", async () => {
    mockApi();
    renderPage(`/wealth/invest?product=${stock.id}`);
    await screen.findByRole("region", { name: "Order" });
    expect(screen.queryByRole("region", { name: "Start a monthly SIP" })).toBeNull();
  });
});
