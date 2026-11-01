import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// The routed shell (ADR-0016) against the full stack: real URLs, reload, back, role areas, the
// skip link and the collapsed navigation at phone width. One customer is registered for the file
// and each test signs in once: registration and login are rate limited per IP (10/h, 10/min) and
// sprint1.spec.ts shares that budget.
const PASSWORD = `${crypto.randomUUID()}Aa1!`;
const EMAIL = `e2e-shell-${Date.now().toString(36)}@example.com`;

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const registered = await request.post("/api/v1/auth/register", {
    data: { email: EMAIL, password: PASSWORD, full_name: "Sam Shell" },
  });
  expect(registered.status()).toBe(201);
});

async function signInAt(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("URLs, reload, back, role areas and the skip link", async ({ page }) => {
  // Signing in on a deep link lands on that page, not on the home page.
  await signInAt(page, "/profile");
  await expect(page.getByRole("heading", { name: "Profile & security" })).toBeVisible();
  await expect(page).toHaveTitle("Profile & security - PBIP");

  await page.reload(); // the refresh cookie restores the session; the URL keeps the page
  await expect(page.getByRole("heading", { name: "Profile & security" })).toBeVisible();

  await page.getByRole("link", { name: "Accounts" }).click();
  await expect(page).toHaveURL(/\/accounts$/);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Profile & security" })).toBeVisible();

  // A customer gets "not found" for staff pages, inside their own shell.
  await page.goto("/staff");
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Staff workspace" })).toHaveCount(0);
  await page.getByRole("link", { name: "Go to your home page" }).click();
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  // The security monitoring page (TASK-099) too, and its API needs a staff bearer token.
  await page.goto("/staff/monitoring");
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Security monitoring" })).toHaveCount(0);
  expect((await page.request.get("/api/v1/fraud/monitoring")).status()).toBe(401);
  await page.getByRole("link", { name: "Go to your home page" }).click();
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();

  // Keyboard users can skip the header and navigation.
  await page.reload();
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
});

test("phone-width navigation collapses behind the menu button", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await signInAt(page, "/");
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav).toBeHidden();

  await page.getByRole("button", { name: "Menu" }).click();
  await expect(nav).toBeVisible();
  await nav.getByRole("link", { name: "Profile & security" }).click();
  await expect(page.getByRole("heading", { name: "Profile & security" })).toBeVisible();
  await expect(nav).toBeHidden();
  // Nothing on the page is wider than the screen.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(360);
});
