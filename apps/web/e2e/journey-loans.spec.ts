import { expect, test } from "@playwright/test";
import { balances, fundCustomer, inr, registerCustomer, signIn } from "./support";

// TASK-077 journey: loan eligibility -> application -> tracking (TASK-046/051, UC-06). Eligibility
// needs regular income (ADR-0043), so scripts/e2e_fund.py credits a INR 1,00,000 salary on the 1st
// of each of the two previous months before the opening balance. The page's first terms are the
// first product (personal loan) at its smallest amount and shortest tenure: INR 25,000 over 12
// months, an EMI far inside 50% of that income, so the pre-check is "Eligible" every run.
test.describe.configure({ timeout: 90_000 });

const FUNDS = 1_000_000; // INR 10,000.00
const SALARY = 10_000_000; // INR 1,00,000.00 a month
const PRINCIPAL = 2_500_000; // INR 25,000.00, the personal loan's minimum

test("a loan is checked, applied for, disbursed and tracked", async ({ page, request }) => {
  const customer = await registerCustomer(request, "loans");
  fundCustomer(customer.email, FUNDS, SALARY);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const before = FUNDS + 2 * SALARY;
  await expect(balances(page).available).toHaveText(inr(before));

  // Eligibility: the pre-check for the page's first terms.
  await page.getByRole("link", { name: "Loans" }).click();
  await expect(page.getByRole("heading", { name: "Loans", level: 1 })).toBeVisible();
  await expect(page.getByText("You have no active loans.")).toBeVisible();
  await expect(page.getByLabel("Loan product")).toHaveValue(/.+/);
  await expect(page.getByLabel("Loan amount")).toHaveAttribute("aria-valuetext", inr(PRINCIPAL));
  const check = page.getByRole("region", { name: "Eligibility pre-check" });
  await expect(check).toContainText("Eligible");
  await expect(check).not.toContainText("Not eligible");

  // Application: review, confirm, and the money arrives.
  const apply = page.getByRole("button", { name: "Apply for this loan" });
  await expect(apply).toBeEnabled();
  await apply.click();
  const review = page.getByRole("dialog", { name: "Check and confirm your loan" });
  await expect(review).toContainText("Personal loan");
  await expect(review).toContainText(inr(PRINCIPAL));
  await expect(review).toContainText("12 months");
  const applied = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/v1/loans/apply",
  );
  await review.getByRole("button", { name: "Confirm and receive the money" }).click();
  expect((await applied).status()).toBe(201);
  const receipt = page.getByRole("region", { name: "Loan approved and disbursed" });
  await expect(receipt).toContainText(inr(PRINCIPAL));
  await expect(receipt).toContainText("First EMI due");

  // Tracking: the loan is listed with its schedule, and it is the API's record (reload).
  await page.reload();
  // The card in "Your loans": since #470 "Loans for you" also has a "Personal loan" offer card.
  const card = page
    .getByRole("region", { name: "Your loans" })
    .getByRole("region", { name: "Personal loan", exact: true });
  await expect(card).toContainText(inr(PRINCIPAL));
  await expect(card).toContainText("0 of 12 EMIs paid");
  await card.getByRole("button", { name: "Show upcoming EMIs" }).click();
  const schedule = card.getByRole("table", { name: "Upcoming EMIs, earliest first" });
  await expect(schedule.getByRole("row").filter({ hasText: "1 of 12" })).toHaveCount(1);
  await expect(
    card.getByRole("progressbar", { name: "Personal loan repayment progress" }),
  ).toHaveAttribute("value", "0");

  // The disbursement is on the account.
  await page.getByRole("link", { name: "Accounts" }).click();
  await expect(balances(page).available).toHaveText(inr(before + PRINCIPAL));
});
