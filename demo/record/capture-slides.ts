import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import {
  addPayee,
  enterSignInCode,
  reviewTransfer,
  signIn,
  staffCode,
  uniqueTag,
} from "../../apps/web/e2e/support";
import { demoPassword } from "./walkthrough-kit";

// The six presentation slide screenshots (1920x1080, demo data only), taken from the running stack
// after the walkthrough is recorded. One step writes: the held-for-review slide sends INR 35,000.00 to a
// payee added a moment earlier (the same story as the walkthrough), which also opens the fraud alert the
// next slide shows. Output: SLIDES_DIR (default ~/Desktop/Rachit/finora-video/slides).

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");
const OUT = process.env.SLIDES_DIR ?? join(homedir(), "Desktop/Rachit/finora-video/slides");
const PASSWORD = demoPassword(REPO);
const REVIEW_AMOUNT = 3_500_000; // INR 35,000.00 to a brand-new payee: held for review

test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1920, height: 1080 }, colorScheme: "light" });

async function ready(page: Page, heading: string | RegExp): Promise<void> {
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  await expect(page.getByText(/^Loading/)).toHaveCount(0, { timeout: 15_000 });
  await page.waitForTimeout(800); // charts and fonts finish painting
}

async function slide(page: Page, name: string): Promise<void> {
  await page.screenshot({
    path: join(OUT, `${name}.png`),
    animations: "disabled",
  });
}

async function signInAs(page: Page, email: string): Promise<void> {
  await signIn(page, email, PASSWORD, "/");
  await enterSignInCode(page, staffCode(email));
  await expect(page.locator("#main h1").first()).toBeVisible();
}

test.beforeAll(() => {
  mkdirSync(OUT, { recursive: true });
});

test("customer slides (Alice)", async ({ page }) => {
  test.setTimeout(6 * 60_000);
  await signInAs(page, "alice@demo.pbip");

  await page.goto("/accounts");
  await ready(page, "My accounts");
  await slide(page, "accounts");

  const payee = await addPayee(page, "Slides");
  await reviewTransfer(page, payee, REVIEW_AMOUNT, `Invoice ${uniqueTag()}`);
  await page.getByRole("button", { name: "Send money" }).click();
  const held = page.getByRole("status").filter({ hasText: "being reviewed" });
  await expect(held).toBeVisible();
  await held.scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  await slide(page, "transfer-review");

  await page.goto("/insights");
  await ready(page, /Spending/);
  await slide(page, "insights");

  await page.goto("/accounts");
  await ready(page, "My accounts");
  await page.getByRole("button", { name: "Ask the Finora assistant" }).click();
  const drawer = page.getByRole("dialog", { name: "Finora assistant" });
  await expect(drawer).toBeVisible();
  await drawer.getByRole("textbox", { name: "Message" }).fill("what's my balance?");
  await drawer.getByRole("button", { name: "Send" }).click();
  const reply = drawer.locator(".assistant-msg--assistant").last();
  await expect(reply).not.toHaveAttribute("aria-busy", "true", {
    timeout: 45_000,
  });
  await expect(reply).toContainText("INR");
  await page.waitForTimeout(600);
  await slide(page, "assistant");
});

test("fraud alert slide (analyst)", async ({ page }) => {
  test.setTimeout(4 * 60_000);
  await signInAs(page, "analyst@demo.pbip");
  await page.goto("/staff/fraud");
  await ready(page, "Fraud alerts");
  await page.getByRole("table").getByRole("link").first().click(); // newest first: the slide's transfer
  await ready(page, /^Risk \d+ of 100/);
  await expect(page.getByText("(SHAP)").first()).toBeVisible();
  await slide(page, "fraud-alert");
});

test("audit log slide (compliance officer)", async ({ page }) => {
  test.setTimeout(4 * 60_000);
  await signInAs(page, "compliance@demo.pbip");
  await page.goto("/staff/audit-log");
  await ready(page, "Audit log");
  await page.getByRole("button", { name: "Verify hash chain" }).click();
  await expect(page.getByText(/intact|verified/i).first()).toBeVisible();
  await page.waitForTimeout(600);
  await slide(page, "audit-log");
});
