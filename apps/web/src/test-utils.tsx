import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { App } from "./App";
import { AuthProvider } from "./auth/AuthContext";

export function urlOf(input: Parameters<typeof fetch>[0]): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

export function renderApp() {
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

export const account = {
  id: "acc-1",
  account_number: "409443369528",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 1_000_000,
  held_minor: 0,
  available_minor: 1_000_000,
};

export function entry(seq: number, amount: number, direction: "CREDIT" | "DEBIT" = "CREDIT") {
  return {
    entry_seq: seq,
    transaction_id: `t-${seq}`,
    direction,
    amount_minor: amount,
    balance_after_minor: 1_000_000,
    transaction_type: "DEPOSIT",
    transaction_status: "POSTED",
    reference: `Ref ${seq}`,
    created_at: "2026-10-01T10:00:00Z",
  };
}
