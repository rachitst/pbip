import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountOut, PayeeOut, TransferOut } from "../api/client";
import { json, urlOf } from "../test-utils";
import { SendMoney } from "./SendMoney";

const savings: AccountOut = {
  id: "acc-1",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 1_000_000,
  held_minor: 0,
  available_minor: 1_000_000,
} as AccountOut;
const current: AccountOut = { ...savings, id: "acc-2", account_type: "CURRENT",
  account_number_masked: "••••••••1111", available_minor: 50_000 }; // prettier-ignore
const payee: PayeeOut = {
  id: "payee-1",
  name: "Bob Demo",
  account_number_masked: "••••••••2621",
  bank_code: "PBIP0001",
  nickname: null,
  status: "VERIFIED",
  verified_at: "2026-01-01T00:00:00Z",
  created_at: "2026-01-01T00:00:00Z",
};

function transfer(status: TransferOut["status"], extra: Partial<TransferOut> = {}): TransferOut {
  return {
    id: "3f2a9c10-0000-4000-8000-000000000001",
    status,
    amount_minor: 30_000,
    currency: "INR",
    message: "server message",
    from_account_id: "acc-1",
    to_account_id: null,
    payee_id: "payee-1",
    reference: null,
    step_up_required: status === "PENDING_STEP_UP",
    created_at: "2026-11-01T10:00:00Z",
    posted_at: null,
    ...extra,
  };
}

function setup(responder: (url: string, body: unknown) => Response) {
  const calls: { url: string; body: unknown; headers: Headers }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ url: urlOf(input), body, headers: new Headers(init?.headers) });
    return Promise.resolve(responder(urlOf(input), body));
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SendMoney accounts={[savings, current]} payees={[payee]} />
    </QueryClientProvider>,
  );
  return calls;
}

async function fillAndConfirm(amount = "300", from = "acc-1") {
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("From account"), from);
  await user.selectOptions(screen.getByLabelText("Payee"), "payee-1");
  await user.type(screen.getByLabelText("Amount (INR)"), amount);
  await user.type(screen.getByLabelText("Reference (optional)"), "rent");
  await user.click(screen.getByRole("button", { name: "Review transfer" }));
  const review = await screen.findByRole("dialog", { name: "Check and confirm" });
  expect(within(review).getByText("Bob Demo (••••••••2621)")).toBeInTheDocument();
  await user.click(within(review).getByRole("button", { name: "Send money" }));
  return user;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SendMoney", () => {
  it("sends from the chosen account after review and shows the receipt", async () => {
    const calls = setup(() => json(201, transfer("POSTED")));
    await fillAndConfirm("300", "acc-2");
    expect(await screen.findByText(/Sent INR 300.00 to Bob Demo/)).toBeInTheDocument();
    const sent = calls.find((c) => c.url.endsWith("/api/v1/transfers"));
    expect(sent?.body).toEqual({ from_account_id: "acc-2", payee_id: "payee-1",
      amount_minor: 30_000, reference: "rent" }); // prettier-ignore
    expect(sent?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("refuses more than the chosen account has available, without calling the API", async () => {
    const calls = setup(() => json(201, transfer("POSTED")));
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("From account"), "acc-2");
    await user.selectOptions(screen.getByLabelText("Payee"), "payee-1");
    await user.type(screen.getByLabelText("Amount (INR)"), "600");
    await user.click(screen.getByRole("button", { name: "Review transfer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "more than the INR 500.00 available",
    );
    expect(calls).toHaveLength(0);
  });

  it("asks for the TOTP on STEP_UP; a wrong code can be retried, a right one completes", async () => {
    let attempt = 0;
    const calls = setup((url) => {
      if (url.endsWith("/step-up")) {
        attempt += 1;
        return attempt === 1
          ? json(422, { title: "invalid verification code", code: "unprocessable", status: 422 })
          : json(200, transfer("POSTED"));
      }
      return json(202, transfer("PENDING_STEP_UP"));
    });
    const user = await fillAndConfirm();
    const modal = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(modal).getByLabelText("Authenticator code"), "111111");
    await user.click(within(modal).getByRole("button", { name: "Confirm transfer" }));
    expect(await within(modal).findByText(/That code did not work/)).toBeInTheDocument();
    await user.type(within(modal).getByLabelText("Authenticator code"), "222222");
    await user.click(within(modal).getByRole("button", { name: "Confirm transfer" }));
    expect(await screen.findByText(/Sent INR 300.00/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.endsWith("/step-up")).map((c) => c.body)).toEqual([
      { code: "111111" },
      { code: "222222" },
    ]);
  });

  it("closes the modal and says the transfer is under review when the server escalates", async () => {
    setup((url) =>
      url.endsWith("/step-up")
        ? json(200, transfer("PENDING_REVIEW"))
        : json(202, transfer("PENDING_STEP_UP")),
    );
    const user = await fillAndConfirm();
    const modal = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(modal).getByLabelText("Authenticator code"), "333333");
    await user.click(within(modal).getByRole("button", { name: "Confirm transfer" }));
    expect(await screen.findByText(/being reviewed for your protection/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).not.toBeInTheDocument();
    });
  });

  it("shows a declined transfer without any reason or score", async () => {
    setup(() => json(403, transfer("DECLINED")));
    await fillAndConfirm();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No money left your account");
    expect(alert.textContent).not.toMatch(/risk|score|reason/i);
  });

  it("leaves a step-up transfer waiting if the customer closes the prompt", async () => {
    setup(() => json(202, transfer("PENDING_STEP_UP")));
    const user = await fillAndConfirm();
    const modal = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(within(modal).getByRole("button", { name: "Not now" }));
    expect(await screen.findByText(/waiting for your authenticator code/)).toBeInTheDocument();
  });
});
