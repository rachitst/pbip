import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountOut, PayeeOut, ScheduledTransferOut } from "../api/client";
import { json, urlOf } from "../test-utils";
import { ScheduledTransfers, formProblem, istDate, statusNote } from "./ScheduledTransfers";

const NOW = Date.parse("2026-11-02T06:00:00Z");
const account: AccountOut = {
  id: "a-1",
  account_number: "100000005678",
  account_number_masked: "••••••••5678",
  account_type: "SAVINGS",
  available_minor: 1_000_000,
  currency: "INR",
  held_minor: 0,
  ledger_balance_minor: 1_000_000,
  status: "ACTIVE",
};
const payee: PayeeOut = {
  id: "p-1",
  name: "Ravi Kumar",
  account_number_masked: "••••1234",
  bank_code: "PBIP0001",
  nickname: null,
  status: "VERIFIED",
  created_at: "2026-10-01T00:00:00Z",
  verified_at: "2026-10-01T00:00:00Z",
};
const order: ScheduledTransferOut = {
  id: "s-1",
  customer_id: "c-1",
  from_account_id: "a-1",
  to_account_id: null,
  payee_id: "p-1",
  amount_minor: 1_500_000,
  currency: "INR",
  reference: "Rent",
  frequency: "MONTHLY",
  status: "ACTIVE",
  start_date: "2026-11-05",
  end_date: null,
  next_run_date: "2026-11-05",
  last_run_date: null,
  last_transaction_id: null,
  failure_count: 0,
  last_failure_reason: null,
  pause_reason: null,
  created_at: "2026-11-01T00:00:00Z",
};

type Handler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

function mockApi(handler: Handler = () => json(404, { title: "x" })) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = urlOf(input);
    if (url === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
    if (url === "/api/v1/payees") return Promise.resolve(json(200, [payee]));
    return Promise.resolve(handler(url, init));
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ScheduledTransfers now={NOW} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("form rules", () => {
  const base = {
    from: "a-1",
    to: "payee:p-1",
    amount: "15000",
    frequency: "MONTHLY" as const,
    start: "2026-11-02",
    end: "",
    reference: "",
  };
  it("accepts a valid schedule and rejects each mistake with a plain sentence", () => {
    const today = istDate(NOW);
    expect(formProblem(base, today)).toBeNull();
    expect(formProblem({ ...base, to: "" }, today)).toMatch(/who to pay/);
    expect(formProblem({ ...base, to: "account:a-1" }, today)).toMatch(/different account/);
    expect(formProblem({ ...base, amount: "12.345" }, today)).toMatch(/amount/);
    expect(formProblem({ ...base, start: "2026-11-01" }, today)).toMatch(/today or later/);
    expect(formProblem({ ...base, start: "2027-12-01" }, today)).toMatch(/within a year/);
    expect(formProblem({ ...base, end: "2026-11-01" }, today)).toMatch(/on or after/);
  });

  it("explains pauses and failures without revealing fraud reasons", () => {
    expect(statusNote({ ...order, status: "PAUSED", pause_reason: "FRAUD_BLOCK" })).toMatch(
      /for your protection/,
    );
    expect(statusNote({ ...order, last_failure_reason: "insufficient_funds" })).toMatch(
      /not enough money/,
    );
    expect(statusNote(order)).toBeNull();
  });
});

describe("ScheduledTransfers page (TASK-021)", () => {
  it("lists upcoming orders with payee, frequency and next date", async () => {
    mockApi(() => json(200, [order, { ...order, id: "s-2", status: "CANCELLED" }]));
    renderPage();
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Ravi Kumar")).toBeInTheDocument();
    expect(within(table).getByText("Every month")).toBeInTheDocument();
    expect(within(table).getByText("5 Nov 2026")).toBeInTheDocument();
    expect(screen.getByText("Cancelled and finished (1)")).toBeInTheDocument();
  });

  it("cancels optimistically through DELETE and restores the row if the API refuses", async () => {
    const user = userEvent.setup();
    let release: (r: Response) => void = () => undefined;
    const fetchMock = mockApi((_url, init) =>
      init?.method === "DELETE"
        ? new Promise<Response>((resolve) => {
            release = resolve;
          })
        : json(200, [order]),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel transfer" }),
    );
    // Gone from the upcoming list before the server has answered.
    expect(await screen.findByText("No upcoming scheduled transfers.")).toBeInTheDocument();
    const del = fetchMock.mock.calls.find(([, init]) => init?.method === "DELETE");
    expect(urlOf(del?.[0] ?? "")).toBe("/api/v1/transfers/scheduled/s-1");
    release(json(409, { code: "conflict", title: "already cancelled" }));
    expect(await screen.findByText("already cancelled")).toBeInTheDocument();
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("validates the form, then creates with an idempotency key", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((url, init) =>
      init?.method === "POST" ? json(201, order) : json(200, []),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Schedule a transfer" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Schedule transfer" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("who to pay");
    await user.selectOptions(within(dialog).getByLabelText(/Pay to/), "payee:p-1");
    await user.type(within(dialog).getByLabelText("Amount (INR)"), "15000");
    await user.click(within(dialog).getByRole("button", { name: "Schedule transfer" }));
    expect(
      await screen.findByText("Scheduled. The first payment is on 5 Nov 2026."),
    ).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({
      from_account_id: "a-1",
      amount_minor: 1_500_000,
      currency: "INR",
      frequency: "MONTHLY",
      start_date: "2026-11-02",
      end_date: null,
      reference: null,
      payee_id: "p-1",
    });
    expect(new Headers(post?.[1]?.headers).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("pauses an active order and hides resume for a fraud-blocked one", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((url) =>
      url.endsWith("/pause")
        ? json(200, { ...order, status: "PAUSED", pause_reason: "CUSTOMER" })
        : json(200, [
            order,
            { ...order, id: "s-3", status: "PAUSED", pause_reason: "FRAUD_BLOCK" },
          ]),
    );
    renderPage();
    await screen.findByRole("table");
    expect(screen.getAllByRole("button", { name: "Pause" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Pause" }));
    await vi.waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => urlOf(input).endsWith("/s-1/pause"))).toBe(
        true,
      );
    });
  });

  it("shows loading errors with a retry", async () => {
    mockApi(() => json(500, { title: "x" }));
    renderPage();
    expect(await screen.findByText(/Could not load your scheduled transfers/)).toBeInTheDocument();
  });
});
