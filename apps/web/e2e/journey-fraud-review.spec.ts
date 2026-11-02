import { expect, test } from "@playwright/test";
import type { Browser, Page } from "@playwright/test";
import {
  ANALYST_EMAIL,
  addPayee,
  balances,
  enterSignInCode,
  fundCustomer,
  inr,
  registerCustomer,
  reviewTransfer,
  signIn,
  staffCode,
  staffPassword,
  uniqueTag,
} from "./support";
import type { Customer } from "./support";

// TASK-077 journey 3: a high-risk transfer is held, a fraud analyst signs in with MFA, finds it in
// the queue (customer name and main reason, #312) and releases it, and the customer sees it posted.
// Second case: the analyst rejects and the held money returns.
// Deterministic hold: a fresh customer WITHOUT an authenticator sends INR 25,000 to a payee added
// seconds ago. NEW_PAYEE_LARGE_AMOUNT asks for a step-up, which a customer with no second factor
// cannot answer, so the payments service holds it for review (payments/service.py). Rules-only (CI)
// and the trained hybrid (checked at every hour of the day) both score it 30-45: never BLOCK.
// The analyst is the seeded analyst@demo.pbip: its password comes from PBIP_DEMO_PASSWORD and its
// code from scripts/totp.py --unused, which never hands out a step the API has already seen.
test.describe.configure({ timeout: 120_000 });

const FUNDS = 6_000_000; // INR 60,000.00
const HELD = 2_500_000; // INR 25,000.00

async function holdATransfer(page: Page, customer: Customer): Promise<string> {
  fundCustomer(customer.email, FUNDS);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const payee = await addPayee(page, "held");
  const reference = `E2E held ${uniqueTag()}`;
  await reviewTransfer(page, payee, HELD, reference);
  await page.getByRole("button", { name: "Send money" }).click();
  // The customer is told it is under review, never why or the score.
  const outcome = page.getByRole("status").filter({ hasText: "being reviewed" });
  await expect(outcome).toContainText(`Your transfer of ${inr(HELD)} to ${payee}`);
  await expect(outcome).not.toContainText(/risk|score|rule/i);
  const account = balances(page);
  await expect(account.available).toHaveText(inr(FUNDS - HELD));
  await expect(account.held).toHaveText(`${inr(HELD)} on hold`);
  await expect(account.ledger).toHaveText(`Ledger: ${inr(FUNDS)}`);
  return reference;
}

/** Signs the seeded analyst in (password + TOTP) in its own browser context and opens the alert
 * raised for ``customer`` from the queue. */
async function openAlertAsAnalyst(browser: Browser, customer: Customer): Promise<Page> {
  const staff = await (await browser.newContext()).newPage();
  await signIn(staff, ANALYST_EMAIL, staffPassword(), "/staff/fraud");
  await enterSignInCode(staff, staffCode(ANALYST_EMAIL));
  await expect(staff.getByRole("heading", { name: "Fraud alerts" })).toBeVisible();
  const row = staff.getByRole("row").filter({ hasText: customer.fullName });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("is a large amount"); // the main reason: NEW_PAYEE_LARGE_AMOUNT
  await expect(row).toContainText(inr(HELD));
  await expect(row).toContainText("Under review");
  await row.getByRole("link").click();
  await expect(staff.getByRole("heading", { name: /^Risk \d+ of 100/ })).toBeVisible();
  await expect(staff.getByRole("region", { name: "Customer" })).toContainText(customer.fullName);
  return staff;
}

async function decide(staff: Page, action: "Release" | "Reject") {
  await staff.getByRole("button", { name: action, exact: true }).click();
  const confirm = staff.getByRole("dialog");
  await expect(confirm).toContainText(inr(HELD));
  await confirm.getByRole("button", { name: action, exact: true }).click();
  await expect(staff.getByText("Decision recorded.")).toBeVisible();
  await expect(staff.getByText(/An analyst resolved this alert/)).toBeVisible();
}

test("a held transfer is released by an analyst and the customer sees it posted", async ({
  page,
  request,
  browser,
}) => {
  const customer = await registerCustomer(request, "release");
  const reference = await holdATransfer(page, customer);

  const staff = await openAlertAsAnalyst(browser, customer);
  await decide(staff, "Release");

  await page.reload();
  const account = balances(page);
  await expect(account.held).toHaveText(`${inr(0)} on hold`);
  await expect(account.ledger).toHaveText(`Ledger: ${inr(FUNDS - HELD)}`);
  await expect(account.available).toHaveText(inr(FUNDS - HELD));
  await account.transactions.click();
  await expect(account.entries.getByRole("row").filter({ hasText: reference })).toContainText(
    "25,000.00",
  );
});

test("a held transfer is rejected by an analyst and the money returns", async ({
  page,
  request,
  browser,
}) => {
  const customer = await registerCustomer(request, "reject");
  const reference = await holdATransfer(page, customer);

  const staff = await openAlertAsAnalyst(browser, customer);
  await decide(staff, "Reject");

  await page.reload();
  const account = balances(page);
  await expect(account.held).toHaveText(`${inr(0)} on hold`);
  await expect(account.available).toHaveText(inr(FUNDS));
  await expect(account.ledger).toHaveText(`Ledger: ${inr(FUNDS)}`);
  await account.transactions.click();
  // Only the opening balance is on the ledger: a rejected transfer never posts an entry.
  await expect(
    account.entries.getByRole("row").filter({ hasText: "E2E opening balance" }),
  ).toHaveCount(1);
  await expect(account.entries.getByRole("row").filter({ hasText: reference })).toHaveCount(0);
});
