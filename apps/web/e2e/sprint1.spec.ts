import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// Sprint 1 customer journey against the full stack (nginx -> API -> PostgreSQL/Redis).
// Each run registers a fresh customer, so the test needs no seed data and leaves others alone.
// Random per run for a throwaway test user (never a real credential); the suffix satisfies the
// letters + digits/symbols rule.
const PASSWORD = `${crypto.randomUUID()}Aa1!`;
const NEW_PASSWORD = `${PASSWORD}-Rotated`;

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("customer opens an account, registers a device and rotates their password", async ({
  page,
  request,
}) => {
  const email = `e2e-${Date.now().toString(36)}@example.com`;
  const registered = await request.post("/api/v1/auth/register", {
    data: { email, password: PASSWORD, full_name: "Erin Endtoend" },
  });
  expect(registered.status()).toBe(201);

  await signIn(page, email, PASSWORD);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();

  // TASK-005: open a checking (current) account.
  await page.getByLabel("Account type").selectOption("CHECKING");
  await page.getByRole("button", { name: "Open account" }).click();
  await expect(page.getByRole("status")).toContainText("opened");
  await expect(page.getByText("Current (checking)").first()).toBeVisible();

  // TASK-008: transaction history (a new account has no entries).
  await page.getByRole("button", { name: "Transactions" }).first().click();
  await expect(page.getByText("No transactions yet.")).toBeVisible();

  // TASK-004: profile and device registration.
  await page.getByRole("button", { name: "Profile & security" }).click();
  await expect(page.getByRole("main").getByText("Erin Endtoend")).toBeVisible();
  await page.getByRole("button", { name: "Register this browser" }).click();
  await expect(page.getByText("This browser is registered.")).toBeVisible();
  await expect(page.getByText(/ on (Linux|Windows|macOS)/).first()).toBeVisible();

  // TASK-006: a wrong current password is refused in place...
  await page.getByLabel("Current password").fill("Not-My-Password-123!");
  await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByLabel("Confirm new password").fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("alert")).toContainText("current password is incorrect");

  // ...the right one rotates it and signs out every session.
  await page.getByLabel("Current password").fill(PASSWORD);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("All your sessions were signed out");

  await signIn(page, email, PASSWORD);
  await expect(page.getByRole("alert")).toBeVisible(); // old password no longer works
  await signIn(page, email, NEW_PASSWORD);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
});

test("another customer's account id is indistinguishable from a missing one", async ({
  request,
}) => {
  const stamp = Date.now().toString(36);
  const tokens: string[] = [];
  for (const who of ["owner", "intruder"]) {
    const email = `e2e-${who}-${stamp}@example.com`;
    await request.post("/api/v1/auth/register", {
      data: { email, password: PASSWORD, full_name: `E2E ${who}` },
    });
    const login = await request.post("/api/v1/auth/login", { data: { email, password: PASSWORD } });
    const body = (await login.json()) as { access_token?: string };
    expect(body.access_token).toBeTruthy();
    tokens.push(body.access_token ?? "");
  }
  const [ownerToken = "", intruderToken = ""] = tokens;
  const accounts = await request.get("/api/v1/accounts", {
    headers: { Authorization: `Bearer ${ownerToken}` },
  });
  const [ownerAccount] = (await accounts.json()) as { id: string }[];
  expect(ownerAccount).toBeDefined();

  const probe = await request.get(`/api/v1/accounts/${ownerAccount?.id ?? ""}/transactions`, {
    headers: { Authorization: `Bearer ${intruderToken}` },
  });
  expect(probe.status()).toBe(404);
});
