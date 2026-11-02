import { expect, test } from "@playwright/test";
import {
  addPayee,
  balances,
  enrolAuthenticator,
  fundCustomer,
  inr,
  registerCustomer,
  reviewTransfer,
  signIn,
  uniqueTag,
} from "./support";

// TASK-077 journey 2: send money through review to a receipt, with the Idempotency-Key checked
// against the API, and see the ledger entry under Accounts -> Transactions. Then the step-up
// variant (TASK-037). Each test has its own fresh, funded customer, so the fraud engine always
// scores the same inputs: a first transfer, to a payee added seconds ago, no failed sign-ins.
//   * INR 100 to a new payee: rule NEW_PAYEE (15 points) -> ALLOW -> POSTED.
//   * INR 25,000 to a new payee: NEW_PAYEE_LARGE_AMOUNT (45 points, and a STEP_UP floor in the
//     hybrid) -> STEP_UP. Checked for the rules-only engine (CI has no model files) and for the
//     trained hybrid at every hour of the day: both give 30-45, never REVIEW (docs/testing/e2e.md).
test.describe.configure({ timeout: 90_000 });

const FUNDS = 5_000_000; // INR 50,000.00
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

test("a transfer goes through review to a receipt, is idempotent and shows in the ledger", async ({
  page,
  request,
}) => {
  const customer = await registerCustomer(request, "sender");
  fundCustomer(customer.email, FUNDS);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const payee = await addPayee(page, "small");
  const reference = `E2E transfer ${uniqueTag()}`;

  await reviewTransfer(page, payee, 10_000, reference);
  const sent = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname === "/api/v1/transfers",
  );
  await page.getByRole("button", { name: "Send money" }).click();
  const transferRequest = await sent;
  const response = await transferRequest.response();
  expect(response?.status()).toBe(201);
  const created = (await response?.json()) as { id: string; status: string };
  expect(created.status).toBe("POSTED");

  // The receipt: the API's outcome and the transfer's own reference.
  const receipt = page.getByRole("status").filter({ hasText: "Sent" });
  await expect(receipt).toContainText(`Sent ${inr(10_000)} to ${payee}`);
  await expect(receipt).toContainText(`Reference ${created.id.slice(0, 8).toUpperCase()}`);

  // One key per intent, and the API honours it: the same key and body replay the original
  // transfer instead of sending the money twice.
  const key = await transferRequest.headerValue("idempotency-key");
  expect(key).toMatch(UUID);
  const replay = await request.post("/api/v1/transfers", {
    headers: {
      Authorization: (await transferRequest.headerValue("authorization")) ?? "",
      "Idempotency-Key": key ?? "",
    },
    data: transferRequest.postDataJSON() as Record<string, unknown>,
  });
  expect(replay.status()).toBe(201);
  expect(replay.headers()["idempotent-replayed"]).toBe("true");
  expect(((await replay.json()) as { id: string }).id).toBe(created.id);

  // Accounts, then Transactions: the debit is on the ledger exactly once.
  const account = balances(page);
  await expect(account.available).toHaveText(inr(FUNDS - 10_000));
  await account.transactions.click();
  const entry = account.entries.getByRole("row").filter({ hasText: reference });
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText("100.00");
  await expect(entry).toContainText(inr(FUNDS - 10_000));
});

test("a large transfer to a new payee asks for the authenticator code, then posts", async ({
  page,
  request,
}) => {
  const customer = await registerCustomer(request, "stepup");
  fundCustomer(customer.email, FUNDS);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  // Step-up needs a second factor; without one the same transfer is held for review (journey 3).
  const app = await enrolAuthenticator(page);
  const payee = await addPayee(page, "large");
  const reference = `E2E step-up ${uniqueTag()}`;

  await reviewTransfer(page, payee, 2_500_000, reference);
  await page.getByRole("button", { name: "Send money" }).click();
  const stepUp = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(stepUp).toContainText(inr(2_500_000));
  // Nothing has moved yet: a step-up transfer holds no money until the code is given.
  await expect(balances(page).available).toHaveText(inr(FUNDS));

  await stepUp.getByLabel("Authenticator code").fill(app.code());
  await stepUp.getByRole("button", { name: "Confirm transfer" }).click();
  await expect(stepUp).toBeHidden();
  await expect(page.getByRole("status").filter({ hasText: "Sent" })).toContainText(
    `Sent ${inr(2_500_000)} to ${payee}`,
  );
  const account = balances(page);
  await expect(account.available).toHaveText(inr(FUNDS - 2_500_000));
  await account.transactions.click();
  await expect(account.entries.getByRole("row").filter({ hasText: reference })).toContainText(
    "25,000.00",
  );
});
