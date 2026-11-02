import { expect, test } from "@playwright/test";
import { addPayee, inr, registerCustomer, signIn, uniqueTag } from "./support";

// TASK-077 journey 4: a standing order is scheduled on /scheduled (TASK-021) and appears in the
// upcoming list. The first payment is a week ahead (India date, as the page counts days), so no
// worker run can execute it during the test and it is "today or later" whatever the clock says.
test.describe.configure({ timeout: 90_000 });

const DAY_MS = 86_400_000;
const IST_OFFSET_MS = 5.5 * 3_600_000;

test("a scheduled transfer is created and listed as upcoming", async ({ page, request }) => {
  const customer = await registerCustomer(request, "scheduler");
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const payee = await addPayee(page, "rent");
  const firstPayment = new Date(Date.now() + IST_OFFSET_MS + 7 * DAY_MS).toISOString().slice(0, 10);
  const reference = `E2E rent ${uniqueTag()}`;

  await page.getByRole("link", { name: "Scheduled transfers" }).click();
  await expect(page.getByRole("heading", { name: "Scheduled transfers" })).toBeVisible();
  await expect(page.getByText("No upcoming scheduled transfers.")).toBeVisible();
  await page.getByRole("button", { name: "Schedule a transfer" }).click();
  const form = page.getByRole("dialog", { name: "Schedule a transfer" });
  const payTo = form.getByLabel("Pay to");
  const option = payTo.locator("option", { hasText: payee });
  await payTo.selectOption((await option.getAttribute("value")) ?? "");
  await form.getByLabel("Amount (INR)").fill("1500.00");
  await form.getByLabel("How often").selectOption({ label: "Every month" });
  await form.getByLabel("First payment").fill(firstPayment);
  await form.getByLabel("Reference (optional)").fill(reference);
  await form.getByRole("button", { name: "Schedule transfer" }).click();

  await expect(form).toBeHidden();
  const nextPayment = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium" }).format(
    new Date(firstPayment),
  );
  await expect(
    page.getByRole("status").filter({ hasText: "The first payment is on" }),
  ).toBeVisible();
  const row = page
    .getByRole("table", { name: "Upcoming scheduled transfers" })
    .getByRole("row")
    .filter({ hasText: payee });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(inr(150_000));
  await expect(row).toContainText("Every month");
  await expect(row).toContainText("Active");
  await expect(row).toContainText(nextPayment);

  // It is the API's record, not just the page's state: it is still there after a reload.
  await page.reload();
  await expect(
    page.getByRole("table", { name: "Upcoming scheduled transfers" }).getByRole("row").filter({
      hasText: payee,
    }),
  ).toContainText(nextPayment);
});
