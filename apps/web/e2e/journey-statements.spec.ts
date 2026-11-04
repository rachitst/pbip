import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { balances, fundCustomer, inr, registerCustomer, signIn } from "./support";

// TASK-077 journey: a customer views their account's transactions (the statement on screen),
// then downloads the period as a PDF and as CSV from Statements (TASK-074, UC-03). The customer
// is new and funded by scripts/e2e_fund.py, so the only entry is that opening deposit, posted
// now: it is inside the page's default period (the 1st of this month to today, India time).
test.describe.configure({ timeout: 90_000 });

const FUNDS = 2_345_600; // INR 23,456.00
const OPENING = "E2E opening balance"; // scripts/e2e_fund.py REFERENCE

test("a statement is viewed on screen and downloaded as PDF and CSV", async ({ page, request }) => {
  const customer = await registerCustomer(request, "statement");
  fundCustomer(customer.email, FUNDS);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();

  // On screen: the account's ledger with the deposit and the running balance.
  const account = balances(page);
  await expect(account.available).toHaveText(inr(FUNDS));
  await account.transactions.click();
  const entry = account.entries.getByRole("row").filter({ hasText: OPENING });
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText(inr(FUNDS));

  // Download: the default period and format (PDF), saved by the browser as a real PDF file.
  await page.getByRole("link", { name: "Statements" }).click();
  await expect(page.getByRole("heading", { name: "Statements" })).toBeVisible();
  await expect(page.getByRole("radio", { name: /PDF/ })).toBeChecked();
  const pdfDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download statement" }).click();
  const pdf = await pdfDownload;
  expect(pdf.suggestedFilename()).toMatch(/\.pdf$/);
  const pdfBytes = await readFile(await pdf.path());
  expect(pdfBytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");
  await expect(page.getByRole("status").filter({ hasText: "Downloaded" })).toContainText(
    pdf.suggestedFilename(),
  );

  // The same period as CSV: the rows are readable, so the content is checked, not just the type.
  await page.getByRole("radio", { name: /CSV/ }).check();
  const csvDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download statement" }).click();
  const csv = await csvDownload;
  expect(csv.suggestedFilename()).toMatch(/\.csv$/);
  const rows = (await readFile(await csv.path(), "utf8")).split(/\r?\n/);
  const deposit = rows.filter((line) => line.includes(OPENING));
  expect(deposit).toHaveLength(1);
  expect(deposit[0]).toMatch(/,23,?456\.00,/); // the credit column
});
