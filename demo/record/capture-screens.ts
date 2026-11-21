import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { enterSignInCode, signIn, staffCode } from "../../apps/web/e2e/support";
import { demoPassword } from "./walkthrough-kit";

// UI redesign (docs/design/ui-redesign-progress.md): one screenshot per page and viewport, as the
// seeded demo users, read-only. Run once before the redesign (SCREENS_SET=before) and once after
// (SCREENS_SET=after); both sets use the same file names so they can be compared side by side.
// Nothing here moves money or changes data: pages are opened, never submitted.

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");
const SET = process.env.SCREENS_SET ?? "after";
const OUT =
  process.env.SCREENS_DIR ?? join(REPO, "docs/design/screenshots", SET);
const PASSWORD = demoPassword(REPO);
const MAX_HEIGHT = 3200; // long pages are cut here so the files stay small

const VIEWPORTS = [
  { tag: "desktop", width: 1440, height: 900 },
  { tag: "phone", width: 360, height: 780 },
] as const;

interface Screen {
  name: string;
  path: string;
  /** Run after the page has loaded, e.g. to open a drawer or select the first row. */
  then?: (page: Page) => Promise<void>;
}

/** Waits for quiet network where the server allows it (a dev proxy can hold a connection open). */
async function quiet(page: Page): Promise<void> {
  await page
    .waitForLoadState("networkidle", { timeout: 5_000 })
    .catch(() => undefined);
}

async function settle(page: Page): Promise<void> {
  await quiet(page);
  await expect(page.locator("#main h1, main h1").first()).toBeVisible();
  await expect(page.getByText(/^Loading/)).toHaveCount(0, { timeout: 15_000 });
  await page.waitForTimeout(600); // charts and fonts finish painting
}

async function shoot(page: Page, name: string, tag: string): Promise<void> {
  const height = await page.evaluate(
    () => document.documentElement.scrollHeight,
  );
  const width = page.viewportSize()?.width ?? 1440;
  await page.screenshot({
    path: join(OUT, `${name}--${tag}.png`),
    fullPage: true,
    clip: { x: 0, y: 0, width, height: Math.min(height, MAX_HEIGHT) },
    animations: "disabled",
  });
}

async function capture(page: Page, screens: Screen[]): Promise<void> {
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    for (const screen of screens) {
      await page.goto(screen.path);
      await settle(page);
      if (screen.then) {
        await screen.then(page);
        await page.waitForTimeout(600);
      }
      await shoot(page, screen.name, viewport.tag);
    }
  }
}

async function signInAs(
  page: Page,
  email: string,
  withCode: boolean,
): Promise<void> {
  await signIn(page, email, PASSWORD, "/");
  if (withCode) await enterSignInCode(page, staffCode(email));
  await expect(page.locator("#main h1").first()).toBeVisible();
}

test.beforeAll(() => {
  mkdirSync(OUT, { recursive: true });
});

// SCREENS_PREPARE=1 on a freshly seeded stack: run three fraud demo lab scenarios first, so the
// alert queue, the alert detail and the dashboards have something to show. This is the only step
// that writes (the lab's own simulated transfers for Carol); the default run writes nothing.
test("prepare: fraud demo lab alerts", async ({ page }) => {
  test.skip(process.env.SCREENS_PREPARE !== "1", "SCREENS_PREPARE=1 only");
  test.setTimeout(6 * 60_000);
  await signInAs(page, "analyst@demo.pbip", true);
  await page.goto("/staff/fraud-lab");
  await page.getByLabel("Customer password").fill(PASSWORD); // typed into the page, never printed
  for (const [code, title] of [
    ["F1", "APP scam"],
    ["F2", "Velocity burst"],
    ["F5", "Amount anomaly"],
  ] as const) {
    await page.getByRole("button", { name: `Run ${code} ${title}` }).click();
    const heading = page.getByRole("heading", {
      name: new RegExp(`^${code} ${title}: `),
    });
    await expect(heading).toHaveText(/: (finished|stopped)$/, {
      timeout: 90_000,
    });
  }
});

test("signed-out pages", async ({ page }) => {
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await page.goto("/");
    await quiet(page);
    await shoot(page, "public-home", viewport.tag);
    await page.goto("/accounts");
    await expect(page.getByLabel("Email")).toBeVisible();
    await shoot(page, "public-sign-in", viewport.tag);
    await page.getByRole("button", { name: "Create an account" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await shoot(page, "public-register", viewport.tag);
    await page.goto("/accounts");
    await signIn(page, "alice@demo.pbip", PASSWORD, "/accounts");
    await expect(
      page.getByLabel("Authenticator or recovery code"),
    ).toBeVisible();
    await shoot(page, "public-sign-in-code", viewport.tag);
  }
});

test("customer pages (Alice)", async ({ page }) => {
  test.setTimeout(10 * 60_000);
  await signInAs(page, "alice@demo.pbip", true);
  await capture(page, [
    { name: "customer-accounts", path: "/accounts" },
    {
      name: "customer-account-transactions",
      path: "/accounts",
      then: async (p) => {
        await p
          .getByRole("button", { name: /transactions/i })
          .first()
          .click();
        await quiet(p);
      },
    },
    { name: "customer-payees", path: "/payees" },
    { name: "customer-bills", path: "/bills" },
    { name: "customer-scheduled", path: "/scheduled" },
    { name: "customer-statements", path: "/statements" },
    { name: "customer-loans", path: "/loans" },
    { name: "customer-wealth", path: "/wealth" },
    { name: "customer-wealth-compare", path: "/wealth/compare" },
    { name: "customer-wealth-invest", path: "/wealth/invest" },
    { name: "customer-wealth-insurance", path: "/wealth/insurance" },
    { name: "customer-insights", path: "/insights" },
    { name: "customer-profile", path: "/profile" },
    {
      name: "customer-notifications",
      path: "/accounts",
      then: async (p) => {
        await p.getByRole("button", { name: /^Notifications/ }).click();
        await quiet(p);
      },
    },
    {
      name: "customer-assistant",
      path: "/accounts",
      then: async (p) => {
        await p.getByRole("button", { name: /^Ask the .* assistant$/ }).click();
        await expect(p.getByRole("dialog")).toBeVisible();
      },
    },
    { name: "customer-staff-page-not-found", path: "/staff/fraud" },
  ]);
});

test("fraud analyst pages", async ({ page }) => {
  test.setTimeout(10 * 60_000);
  await signInAs(page, "analyst@demo.pbip", true);
  await capture(page, [
    { name: "staff-overview", path: "/staff" },
    { name: "staff-security-monitoring", path: "/staff/monitoring" },
    { name: "staff-fraud-alerts", path: "/staff/fraud" },
    {
      name: "staff-fraud-alert-detail",
      path: "/staff/fraud",
      then: async (p) => {
        await p.getByRole("button", { name: "All", exact: true }).click(); // the open queue may be empty
        await quiet(p);
        const first = p.getByRole("table").getByRole("link").first();
        if ((await first.count()) === 0) return; // an empty queue: the list screenshot says so
        await first.click();
        await expect(
          p.getByRole("heading", { level: 1, name: /^Risk \d+ of 100/ }),
        ).toBeVisible();
        await settle(p);
      },
    },
    { name: "staff-fraud-dashboard", path: "/staff/fraud-dashboard" },
    { name: "staff-fraud-demo-lab", path: "/staff/fraud-lab" },
    { name: "staff-sign-in-security", path: "/staff/security" },
  ]);
});

test("compliance officer pages", async ({ page }) => {
  test.setTimeout(6 * 60_000);
  await signInAs(page, "compliance@demo.pbip", true);
  await capture(page, [
    { name: "staff-compliance", path: "/staff/compliance" },
    { name: "staff-audit-log", path: "/staff/audit-log" },
  ]);
});

test("relationship manager pages", async ({ page }) => {
  test.setTimeout(6 * 60_000);
  await signInAs(page, "rm@demo.pbip", true);
  await capture(page, [
    { name: "staff-customer-insights", path: "/staff/insights" },
    { name: "staff-behavioural-analytics", path: "/staff/behaviour" },
  ]);
});

test("administrator pages", async ({ page }) => {
  test.setTimeout(6 * 60_000);
  await signInAs(page, "admin@demo.pbip", true);
  await capture(page, [{ name: "staff-administration", path: "/staff/admin" }]);
});
