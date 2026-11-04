import { expect, test } from "@playwright/test";
import { balances, fundCustomer, inr, registerCustomer, signIn, uniqueTag } from "./support";

// TASK-077 journey: pay a utility bill (TASK-020) through the review step to a receipt, see it in
// the recent bill payments and as a debit on the account. The biller is seeded reference data
// (migration 0008). A fresh, funded customer and a small amount: the fraud engine scores the same
// inputs every run (a first payment, no history, no failed sign-ins) and allows it.
test.describe.configure({ timeout: 90_000 });

const FUNDS = 5_000_000; // INR 50,000.00
const BILL = 125_000; // INR 1,250.00
const BILLER = "Maharashtra State Electricity Board";

test("a bill is paid through review to a receipt and shows in history", async ({
  page,
  request,
}) => {
  const customer = await registerCustomer(request, "bills");
  fundCustomer(customer.email, FUNDS);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const consumer = `E2E${uniqueTag()}`.slice(0, 20);

  await page.getByRole("link", { name: "Pay bills" }).click();
  await expect(page.getByRole("heading", { name: "Pay bills" })).toBeVisible();
  await expect(page.getByText("No bill payments yet.")).toBeVisible();
  const biller = page.getByLabel("Biller");
  const option = biller.locator("option", { hasText: BILLER });
  await biller.selectOption((await option.getAttribute("value")) ?? "");
  await page.getByLabel("Consumer number").fill(consumer);
  await page.getByLabel("Amount (INR)").fill((BILL / 100).toFixed(2));
  await page.getByRole("button", { name: "Review payment" }).click();

  const review = page.getByRole("dialog", { name: "Check and confirm" });
  await expect(review).toContainText(BILLER);
  await expect(review).toContainText(consumer);
  await expect(review).toContainText(inr(BILL));
  const paid = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/v1/bills/pay",
  );
  await review.getByRole("button", { name: "Pay bill" }).click();
  expect((await paid).status()).toBe(201);

  const receipt = page.getByRole("region", { name: "Bill paid" });
  await expect(receipt).toContainText(inr(BILL));
  await expect(receipt).toContainText(consumer);
  await expect(receipt).toContainText(inr(FUNDS - BILL));
  const row = page.getByRole("row").filter({ hasText: consumer });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(inr(BILL));
  await expect(row).toContainText(BILLER);

  // The ledger agrees: the account shows the debit once.
  await page.getByRole("link", { name: "Accounts" }).click();
  await expect(balances(page).available).toHaveText(inr(FUNDS - BILL));
});
