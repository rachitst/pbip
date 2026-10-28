import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AuthProvider } from "../auth/AuthContext";

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

describe("accounts page", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, [payee]));
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

  it("restores the session, shows balances and the amount on hold", async () => {
    renderApp();
    expect(await screen.findByLabelText("available balance")).toHaveTextContent("INR 9,750.00");
    expect(screen.getByLabelText("ledger balance")).toHaveTextContent("Ledger: INR 10,000.00");
    expect(screen.getByLabelText("held funds")).toHaveTextContent("INR 250.00 on hold");
    expect(screen.getByText("INR 250.00 on hold")).toBeInTheDocument();
  });

  it("sends an exact minor-unit amount with an Idempotency-Key header", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.selectOptions(await screen.findByLabelText("Payee"), "payee-1");
    await user.type(screen.getByLabelText("Amount (INR)"), "125.50");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Transfer completed");
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
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("valid amount");
    expect(fetchMock.mock.calls.some(([url]) => urlOf(url).endsWith("/transfers"))).toBe(false);
  });
});
