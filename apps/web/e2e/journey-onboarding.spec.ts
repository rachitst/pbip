import { expect, test } from "@playwright/test";
import {
  balances,
  enrolAuthenticator,
  enterSignInCode,
  fundCustomer,
  inr,
  newPassword,
  signIn,
  uniqueTag,
} from "./support";

// TASK-077 journey 1: a customer registers in the UI (#201), enrols MFA in Profile & security,
// signs out, signs back in with a code, and receives an opening balance.
// Opening balance: PBIP has no customer-facing deposit, so the money is posted by
// scripts/e2e_fund.py (a ledger deposit) and the test checks the customer then sees it.
test.describe.configure({ timeout: 90_000 });

const OPENING_BALANCE = 7_654_321; // INR 76,543.21

test("a customer registers, enrols an authenticator, signs back in with a code and is funded", async ({
  page,
}) => {
  const tag = uniqueTag();
  const email = `e2e-onboard-${tag}@example.com`;
  const fullName = `Olive Onboard ${tag}`;
  const password = newPassword();

  await page.goto("/");
  await page.getByRole("button", { name: "Create an account" }).click();
  await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
  await page.getByLabel("Full name").fill(fullName);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByLabel("Confirm password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  // Registration signs the new customer straight in; a savings account is opened with it.
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  await expect(balances(page).available).toHaveText(inr(0));

  const app = await enrolAuthenticator(page);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  // The password alone is no longer enough: the second step asks for the app's code.
  await signIn(page, email, password);
  await expect(page.getByLabel("Authenticator or recovery code")).toBeVisible();
  await enterSignInCode(page, app.code());
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  await page.getByRole("link", { name: "Profile & security" }).click();
  await expect(page.getByText("An authenticator app is set up.")).toBeVisible();

  fundCustomer(email, OPENING_BALANCE);
  await page.getByRole("link", { name: "Accounts" }).click();
  await page.reload(); // the session survives a reload (refresh cookie); the balance is re-read
  const account = balances(page);
  await expect(account.available).toHaveText(inr(OPENING_BALANCE));
  await account.transactions.click();
  await expect(
    account.entries.getByRole("row").filter({ hasText: "E2E opening balance" }),
  ).toContainText("76,543.21");
});
