import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { customerMe, noRecommendations } from "../test-utils";

function urlOf(input: Parameters<typeof fetch>[0]): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const account = {
  id: "acc-1",
  account_number: "409443369528",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 1_000_000,
  held_minor: 25_000,
  available_minor: 975_000,
};
const payee = {
  id: "payee-1",
  name: "Bob Demo",
  account_number_masked: "••••••••3050",
  bank_code: "PBIP0001",
  status: "VERIFIED",
  nickname: null,
  created_at: "2026-01-01T00:00:00Z",
  verified_at: "2026-01-01T00:00:00Z",
};

const creditLine = {
  product_id: "00000000-0000-4000-a000-000000000003",
  product_type: "LOAN",
  title: "Personal credit line",
  description: "Borrow as needed up to an agreed limit, subject to a credit check.",
  score: 0.64,
  reasons: ["Loan repayments take 12.0% of your income, within the 45% limit."],
  model_version: "rules-v1",
  cta_label: "Check eligibility",
};

describe("accounts page", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let recommendations: unknown = noRecommendations;

  beforeEach(() => {
    recommendations = noRecommendations;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/accounts/summary")) {
        return Promise.resolve(
          json(200, {
            totals: [
              {
                currency: "INR",
                ledger_balance_minor: 1_000_000,
                held_minor: 25_000,
                available_minor: 975_000,
                account_count: 1,
                frozen_account_count: 0,
              },
            ],
          }),
        );
      }
      if (url.endsWith("/payees")) return Promise.resolve(json(200, [payee]));
      if (url.endsWith("/recommendations")) return Promise.resolve(json(200, recommendations));
      if (url.endsWith("/events/batch")) return Promise.resolve(json(202, {}));
      if (url.endsWith("/transfers") && init?.method === "POST") {
        return Promise.resolve(
          json(201, {
            id: "t-1",
            status: "POSTED",
            amount_minor: 12_550,
            currency: "INR",
            message: "Transfer completed",
          }),
        );
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function renderApp() {
    return render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <AuthProvider>
          <App />
        </AuthProvider>
      </QueryClientProvider>,
    );
  }

  it("sends product_clicked with the catalogue product id when a recommendation is followed (TASK-112)", async () => {
    recommendations = { ...noRecommendations, recommendations: [creditLine] };
    const user = userEvent.setup();
    renderApp();
    await user.click(
      await screen.findByRole("button", { name: "See our loans: Personal credit line" }),
    );
    window.dispatchEvent(new Event("pagehide"));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => urlOf(input).endsWith("/events/batch"))).toBe(
        true,
      );
    });
    const sent = fetchMock.mock.calls
      .filter(([input]) => urlOf(input).endsWith("/events/batch"))
      .flatMap(
        ([, init]) =>
          (
            JSON.parse(init?.body as string) as {
              events: { event_type: string; page_url?: string; properties?: object }[];
            }
          ).events,
      );
    expect(sent.find((e) => e.event_type === "product_clicked")).toMatchObject({
      page_url: "/accounts",
      properties: { product_id: creditLine.product_id },
    });
  });

  it("restores the session, shows balances and the amount on hold", async () => {
    renderApp();
    expect(await screen.findByLabelText("available balance")).toHaveTextContent("INR 9,750.00");
    expect(screen.getByLabelText("ledger balance")).toHaveTextContent("Ledger: INR 10,000.00");
    expect(screen.getByLabelText("held funds")).toHaveTextContent("INR 250.00 on hold");
    expect(screen.getByText("INR 250.00 on hold")).toBeInTheDocument();
  });

  it("puts the server-computed total balance above the account list (TASK-109)", async () => {
    renderApp();
    const total = await screen.findByLabelText("total available INR");
    expect(total).toHaveTextContent("INR 9,750.00");
    expect(screen.getByLabelText("total ledger INR")).toHaveTextContent("Ledger: INR 10,000.00");
    const firstAccount = await screen.findByLabelText("available balance");
    expect(
      total.compareDocumentPosition(firstAccount) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("sends an exact minor-unit amount with an Idempotency-Key header", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.selectOptions(await screen.findByLabelText("Payee"), "payee-1");
    await user.type(screen.getByLabelText("Amount (INR)"), "125.50");
    await user.click(screen.getByRole("button", { name: "Review transfer" }));
    await user.click(await screen.findByRole("button", { name: "Send money" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Sent INR 125.50");
    const call = fetchMock.mock.calls.find(([url]) => urlOf(url).endsWith("/transfers"));
    const init = call?.[1];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(JSON.parse(init?.body as string) as unknown).toMatchObject({
      amount_minor: 12_550,
      payee_id: "payee-1",
    });
  });

  it("refuses an invalid amount without calling the API", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.selectOptions(await screen.findByLabelText("Payee"), "payee-1");
    await user.type(screen.getByLabelText("Amount (INR)"), "12.345");
    await user.click(screen.getByRole("button", { name: "Review transfer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("valid amount");
    expect(fetchMock.mock.calls.some(([url]) => urlOf(url).endsWith("/transfers"))).toBe(false);
  });
});
