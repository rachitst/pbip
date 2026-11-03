import { expect, test } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";
import {
  enterSignInCode,
  inr,
  newPassword,
  signIn,
  staffCode,
  staffPassword,
  uniqueTag,
} from "./support";

// TASK-108 (FR-05, ADR-0031): the administration screens at /staff/admin, end to end.
// The administrator and the relationship manager are the MFA-enrolled staff seeded by
// scripts/seed_dev.py (password PBIP_DEMO_PASSWORD, codes from scripts/totp.py --unused).
// Settings are global state shared with the other specs running in parallel, so the journey changes
// only the standing-order cap, by one, and puts the value back: the only spec that creates standing
// orders (journey-scheduled) makes ONE per fresh customer, which no value in the bounds (1 to 200)
// can refuse. The daily and per-transfer limits, which the money journeys depend on, are only read.
test.describe.configure({ timeout: 120_000 });

const ADMIN_EMAIL = "admin@demo.pbip";
const RM_EMAIL = "rm@demo.pbip";
const SCHEDULED_CAP = "Scheduled transfers per customer";

async function signInAsStaff(page: Page, email: string) {
  await signIn(page, email, staffPassword(), "/staff/admin");
  await enterSignInCode(page, staffCode(email));
  await expect(page.getByRole("heading", { name: "Administration", exact: true })).toBeVisible();
}

/** A settings row by its label. The row header also holds the description, hence the prefix match. */
function settingRow(table: Locator, label: string): Locator {
  return table
    .getByRole("row")
    .filter({ has: table.page().getByRole("rowheader", { name: new RegExp(`^${label}`) }) });
}

/** Columns after the row header: in force, default, allowed range, last change, actions. */
function cells(row: Locator) {
  const cell = row.getByRole("cell");
  return {
    inForce: cell.nth(0),
    defaultValue: cell.nth(1),
    range: cell.nth(2),
    lastChange: cell.nth(3),
  };
}

async function changeSetting(panel: Locator, label: string, value: number, reason: string) {
  const name = `Change ${label.toLowerCase()}`;
  await panel.getByRole("button", { name, exact: true }).click();
  const dialog = panel.page().getByRole("dialog", { name });
  await dialog.getByLabel("New value", { exact: true }).fill(String(value));
  await dialog.getByLabel("Reason", { exact: true }).fill(reason);
  await dialog.getByRole("button", { name: "Save change", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(panel.getByRole("status")).toHaveText(
    `${label} is now ${String(value)}. It applies to the next payment.`,
  );
}

test("an administrator sees the allow-listed settings and changes one with a reason", async ({
  page,
}) => {
  await signInAsStaff(page, ADMIN_EMAIL);
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: "Administration", exact: true })).toBeVisible();
  // The session passed MFA, so the "sign in again with your code" warning is not shown.
  await expect(page.getByText("These tools need a session verified")).toHaveCount(0);

  const panel = page.getByRole("region", { name: "Application settings" });
  const table = panel.getByRole("table", { name: "Runtime-configurable settings" });
  // Exactly the three ADR-0031 business limits (plus the header row): no secret or security setting.
  await expect(table.getByRole("row")).toHaveCount(4);
  const daily = cells(settingRow(table, "Daily transfer limit"));
  await expect(daily.defaultValue).toHaveText(inr(50_000_000));
  await expect(daily.inForce).toContainText(inr(50_000_000));
  await expect(daily.range).toHaveText(`${inr(10_000)} to ${inr(1_000_000_000)}`);
  const perTransfer = cells(settingRow(table, "Per-transfer limit"));
  await expect(perTransfer.defaultValue).toHaveText(inr(20_000_000));
  await expect(perTransfer.inForce).toContainText(inr(20_000_000));
  await expect(perTransfer.range).toHaveText(`${inr(10_000)} to ${inr(1_000_000_000)}`);
  const cap = cells(settingRow(table, SCHEDULED_CAP));
  await expect(cap.defaultValue).toHaveText("20");
  await expect(cap.range).toHaveText("1 to 200");

  // Put back whatever was in force (the environment default on a fresh stack), even if a later step
  // fails. A failure before the save leaves nothing to restore; one between the server saving and
  // the success message would leave the cap one lower, which no other spec can notice.
  const original = Number(/^\d+/.exec((await cap.inForce.textContent()) ?? "")?.[0]);
  expect(original).toBeGreaterThanOrEqual(1);
  const lowered = original > 1 ? original - 1 : original + 1;
  let changed = false;
  try {
    // The client refuses a reason under 10 characters before asking the server.
    await panel
      .getByRole("button", { name: `Change ${SCHEDULED_CAP.toLowerCase()}`, exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: `Change ${SCHEDULED_CAP.toLowerCase()}` });
    await dialog.getByLabel("New value", { exact: true }).fill(String(lowered));
    await dialog.getByLabel("Reason", { exact: true }).fill("too short");
    await dialog.getByRole("button", { name: "Save change", exact: true }).click();
    await expect(dialog.getByText("Give a reason of at least 10 characters.")).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).toBeHidden();

    await changeSetting(panel, SCHEDULED_CAP, lowered, `E2E admin journey ${uniqueTag()}`);
    changed = true;
    // The table is re-read from the server after the change.
    await expect(cap.inForce).toHaveText(`${String(lowered)} Changed`);
    await expect(cap.defaultValue).toHaveText("20");
    await expect(cap.lastChange).not.toHaveText("Never");
  } finally {
    if (changed) {
      await changeSetting(
        panel,
        SCHEDULED_CAP,
        original,
        `E2E admin journey restore ${uniqueTag()}`,
      );
      await expect(cap.inForce).toContainText(String(original));
    }
  }
});

test("an administrator creates a staff user and clears their sign-in lockout", async ({
  page,
  request,
}) => {
  await signInAsStaff(page, ADMIN_EMAIL);
  const tag = uniqueTag();
  const email = `e2e-staff-${tag}@example.com`;
  const password = newPassword();

  const create = page.getByRole("region", { name: "Create a staff user" });
  await create.getByLabel("Work e-mail", { exact: true }).fill(email);
  await create.getByLabel("Full name", { exact: true }).fill(`E2E Staff ${tag}`);
  await create.getByLabel("Initial password", { exact: true }).fill(password);
  // No role is preselected: the button stays disabled until one is chosen.
  await expect(create.getByRole("button", { name: "Create user", exact: true })).toBeDisabled();
  await create.getByRole("checkbox", { name: "Compliance officer", exact: true }).check();
  await create.getByRole("button", { name: "Create user", exact: true }).click();
  const created = create.getByRole("status");
  await expect(created).toContainText(`Created ${email} as Compliance officer.`);
  const userId = ((await created.locator("code").textContent()) ?? "").trim();
  expect(userId).toMatch(/^[0-9a-f-]{36}$/);

  // Five wrong passwords lock the new account (login_max_failures); then even the right one is refused.
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const guess = newPassword(); // a fresh random value, never the account's
    const failed = await request.post("/api/v1/auth/login", { data: { email, password: guess } });
    expect(failed.status()).toBe(401);
  }
  const locked = await request.post("/api/v1/auth/login", { data: { email, password } });
  expect(locked.status()).toBe(423);

  const lockout = page.getByRole("region", { name: "Sign-in lockout" });
  await lockout.getByLabel("User id", { exact: true }).fill(userId);
  await lockout.getByRole("button", { name: "Inspect", exact: true }).click();
  await expect(lockout.getByText("Locked", { exact: true })).toBeVisible();
  // The closed confirm dialog in the same panel has the same rows, hence the visible filter.
  const failures = lockout
    .locator(".details__row")
    .filter({ visible: true })
    .filter({ has: page.getByText("Failed sign-ins", { exact: true }) });
  await expect(failures).toHaveText("Failed sign-ins5");

  await lockout.getByRole("button", { name: "Clear lockout", exact: true }).click();
  const confirm = page.getByRole("dialog", { name: "Clear this lockout?" });
  await expect(confirm).toContainText(userId);
  await expect(confirm).toContainText("Failed sign-ins5");
  await confirm.getByRole("button", { name: "Clear lockout", exact: true }).click();
  await expect(confirm).toBeHidden();
  await expect(lockout.getByRole("status")).toContainText(
    "The lockout was cleared; 5 failed sign-ins reset",
  );
  await expect(lockout.getByText("Not locked", { exact: true })).toBeVisible();

  const signedIn = await request.post("/api/v1/auth/login", { data: { email, password } });
  expect(signedIn.status()).toBe(200);
});

test("a non-admin staff user gets no administration link, a restricted page and 403", async ({
  page,
}) => {
  // The bearer the app itself sends once signed in, to show the API refuses the role too.
  let bearer = "";
  page.on("request", (sent) => {
    const header = sent.headers()["authorization"];
    if (sent.url().includes("/api/v1/") && header?.startsWith("Bearer ")) bearer = header;
  });
  await signInAsStaff(page, RM_EMAIL);
  await expect(
    page.getByText("Access restricted: administration needs the Administrator role."),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Application settings" })).toHaveCount(0);
  await expect(
    page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Administration" }),
  ).toHaveCount(0);

  expect(bearer).not.toBe("");
  const headers = { Authorization: bearer };
  // "forbidden" (the role), not "mfa_required": the session passed MFA and is still refused.
  const read = await page.request.get("/api/v1/admin/settings", { headers });
  expect(read.status()).toBe(403);
  expect(((await read.json()) as { code?: string }).code).toBe("forbidden");
  const change = await page.request.put(
    "/api/v1/admin/settings/max_active_scheduled_transfers_per_customer",
    { headers, data: { value: 19, reason: "E2E: a relationship manager must be refused" } },
  );
  expect(change.status()).toBe(403);
  expect(((await change.json()) as { code?: string }).code).toBe("forbidden");
});
