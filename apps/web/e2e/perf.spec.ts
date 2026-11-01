import { expect, test } from "@playwright/test";
import type { Page, TestInfo } from "@playwright/test";

// TASK-102: page-load and login budgets from the use case document, section 8, measured in a real
// browser against the full stack. Fails the job when a budget is missed; every measurement is
// attached to the report (perf-budgets.json) with the environment it ran on.
//   page load < 2 s: a full navigation until the page's main heading is visible. "/" (signed out) runs first in a
//                    fresh browser context (empty cache); the signed-in pages are full reloads (bundle
//                    from the HTTP cache, session restored from the refresh cookie, data fetched)
//   login     < 3 s: from pressing "Sign in" until the accounts page heading is visible
// One customer is registered for the file: registration is rate limited per IP (10/h), shared with
// the other E2E files.
const PAGE_BUDGET_MS = 2000;
// Waits get a generous ceiling so a slow page is MEASURED and then fails on its budget with the real
// number, instead of failing on Playwright's 5 s default with no timing at all.
const WAIT = { timeout: 20_000 };
const LOGIN_BUDGET_MS = 3000;
const PASSWORD = `${crypto.randomUUID()}Aa1!`;
const EMAIL = `e2e-perf-${Date.now().toString(36)}@example.com`;

type Measurement = { name: string; ms: number; budgetMs: number; navigationMs?: number };
const results: Measurement[] = [];

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const registered = await request.post("/api/v1/auth/register", {
    data: { email: EMAIL, password: PASSWORD, full_name: "Pat Perf" },
  });
  expect(registered.status()).toBe(201);
});

async function navigationMs(page: Page): Promise<number | undefined> {
  return page.evaluate(() => {
    const [nav] = performance.getEntriesByType("navigation") as PerformanceNavigationTiming[];
    return nav && nav.loadEventEnd > 0 ? Math.round(nav.loadEventEnd) : undefined;
  });
}

async function timeToHeading(page: Page, path: string): Promise<Measurement> {
  const started = Date.now();
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible(WAIT);
  return {
    name: `page ${path}`,
    ms: Date.now() - started,
    budgetMs: PAGE_BUDGET_MS,
    navigationMs: await navigationMs(page),
  };
}

async function report(testInfo: TestInfo): Promise<void> {
  const environment = {
    baseURL: testInfo.project.use.baseURL,
    ci: Boolean(process.env.CI),
    runner: process.env.RUNNER_NAME ?? process.env.HOSTNAME ?? "local",
    os: process.platform,
    cpus: (await import("node:os")).cpus().length,
    browser: testInfo.project.name,
  };
  await testInfo.attach("perf-budgets.json", {
    body: JSON.stringify({ environment, results }, null, 2),
    contentType: "application/json",
  });
}

test("login completes within 3 s and customer pages load within 2 s", async ({
  page,
}, testInfo) => {
  results.push(await timeToHeading(page, "/")); // signed out: the sign-in page

  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  const started = Date.now();
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible(WAIT);
  results.push({ name: "login", ms: Date.now() - started, budgetMs: LOGIN_BUDGET_MS });

  for (const path of ["/accounts", "/insights", "/profile"]) {
    results.push(await timeToHeading(page, path));
  }
  await report(testInfo);

  for (const m of results) {
    expect
      .soft(m.ms, `${m.name} took ${m.ms} ms (budget ${m.budgetMs} ms)`)
      .toBeLessThan(m.budgetMs);
  }
});
