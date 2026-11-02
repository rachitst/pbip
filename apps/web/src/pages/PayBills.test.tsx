import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountOut, BillPaymentOut, BillerOut } from "../api/client";
import { json, urlOf } from "../test-utils";
import { clickstream } from "../telemetry/tracker";
import { PayBills } from "./PayBills";

const account: AccountOut = {
  id: "a-1",
  account_number: "100000005678",
  account_number_masked: "XXXXXXXX5678",
  account_type: "SAVINGS",
  available_minor: 500_000,
  currency: "INR",
  held_minor: 0,
  ledger_balance_minor: 500_000,
  status: "ACTIVE",
};
const biller: BillerOut = {
  id: "b-1",
  name: "City Power",
  category: "ELECTRICITY",
  biller_code: "CPWR01",
  status: "ACTIVE",
};
const paid: BillPaymentOut = {
  id: "bp-1",
  customer_id: "c-1",
  account_id: "a-1",
  biller_id: "b-1",
  biller_name: "City Power",
  biller_code: "CPWR01",
  transaction_id: "t-1",
  consumer_reference: "CN-778899",
  amount_minor: 125_050,
  currency: "INR",
  status: "POSTED",
  reference: "BILL-7F3A2C",
  new_balance_minor: 374_950,
  created_at: "2026-11-02T10:00:00Z",
  message: "Bill paid",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PayBills />
    </QueryClientProvider>,
  );
}

function mockApi(pay: () => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = urlOf(input);
    if (url === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
    if (url === "/api/v1/bills/billers") return Promise.resolve(json(200, [biller]));
    if (url.startsWith("/api/v1/bills/payments")) return Promise.resolve(json(200, []));
    return Promise.resolve(pay());
  });
}

async function fillAndConfirm(user: ReturnType<typeof userEvent.setup>, amount: string) {
  await user.selectOptions(await screen.findByLabelText("Biller"), "b-1");
  await user.type(screen.getByLabelText(/Consumer number/), "CN-778899");
  await user.type(screen.getByLabelText("Amount (INR)"), amount);
  await user.click(screen.getByRole("button", { name: "Review payment" }));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PayBills (TASK-020)", () => {
  it("pays in integer minor units with one idempotency key and shows the receipt", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi(() => json(201, paid));
    const track = vi.spyOn(clickstream, "track");
    renderPage();
    await fillAndConfirm(user, "1250.50");
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Pay bill" }));
    const receipt = await screen.findByRole("region", { name: "Bill paid" });
    expect(within(receipt).getByText("BILL-7F3A2C")).toBeInTheDocument();
    expect(within(receipt).getByText(/3,749\.50/)).toBeInTheDocument();
    expect(within(receipt).getByText(/1,250\.50/)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([input]) => urlOf(input) === "/api/v1/bills/pay");
    expect(JSON.parse(call?.[1]?.body as string)).toEqual({
      from_account_id: "a-1",
      biller_id: "b-1",
      consumer_reference: "CN-778899",
      amount_minor: 125_050,
    });
    const headers = new Headers(call?.[1]?.headers);
    expect(headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(track.mock.calls).toEqual([["transfer_initiated", { transfer_kind: "BILL_PAYMENT" }]]);
  });

  it("reuses the same idempotency key when the same confirmed payment is retried", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const fetchMock = mockApi(() => {
      attempts += 1;
      return attempts === 1 ? json(503, { title: "busy" }) : json(201, paid);
    });
    renderPage();
    await fillAndConfirm(user, "10");
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Pay bill" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("could not be paid");
    await user.click(within(dialog).getByRole("button", { name: "Pay bill" }));
    await screen.findByRole("region", { name: "Bill paid" });
    const keys = fetchMock.mock.calls
      .filter(([input]) => urlOf(input) === "/api/v1/bills/pay")
      .map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("says a held payment is on hold and a declined one was not paid", async () => {
    const user = userEvent.setup();
    mockApi(() => json(202, { ...paid, status: "PENDING_REVIEW" }));
    const { unmount } = renderPage();
    await fillAndConfirm(user, "10");
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Pay bill" }),
    );
    expect(await screen.findByText(/being reviewed for your protection/)).toBeInTheDocument();
    unmount();
    vi.restoreAllMocks();
    mockApi(() => json(403, { ...paid, status: "DECLINED" }));
    renderPage();
    await fillAndConfirm(user, "10");
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Pay bill" }),
    );
    expect(await screen.findByText(/No money left your account/)).toBeInTheDocument();
  });

  it("refuses more than the available balance before calling the API", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi(() => json(201, paid));
    renderPage();
    await fillAndConfirm(user, "6000");
    expect(screen.getByRole("alert")).toHaveTextContent("more than the");
    expect(fetchMock.mock.calls.some(([input]) => urlOf(input) === "/api/v1/bills/pay")).toBe(
      false,
    );
  });

  it("shows an error state when billers cannot load", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        urlOf(input) === "/api/v1/accounts" ? json(200, [account]) : json(500, { title: "x" }),
      ),
    );
    renderPage();
    expect(
      await screen.findByText(/Could not load your accounts or the billers/),
    ).toBeInTheDocument();
  });
});
