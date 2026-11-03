import { execFileSync } from "node:child_process";
import { createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

// Shared helpers for the TASK-077 critical journeys. Every journey makes its own customers (unique
// per run) so no test depends on another test, on the order of the specs or on leftover data.

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
// Where `docker compose exec` finds the running stack (the checkout that started it).
const COMPOSE_DIR = process.env.E2E_COMPOSE_DIR ?? REPO_ROOT;

/** Seeded staff (scripts/seed_dev.py). Their password is the one the seed was run with. */
export const ANALYST_EMAIL = "analyst@demo.pbip";
export const ADMIN_EMAIL = "admin@demo.pbip";
export const RM_EMAIL = "rm@demo.pbip";

export function uniqueTag(): string {
  return `${Date.now().toString(36)}${randomUUID().slice(0, 6)}`;
}

/** A throwaway password generated per run (never a real credential); the suffix satisfies the
 * letters + digits/symbols rule. The random hex is split into groups of four: a raw uuid can hold
 * a 5-character run such as "23456", which the keyboard-sequence rule rejects (about 1 run in
 * 4,000; it failed the BOLA journey on PR #344). */
export function newPassword(): string {
  const hex = randomUUID().replace(/-/g, "");
  return `${(hex.match(/.{4}/g) ?? []).join("-")}-Aa1!`;
}

export function staffPassword(): string {
  const password = process.env.PBIP_DEMO_PASSWORD;
  if (!password) {
    throw new Error(
      "PBIP_DEMO_PASSWORD is not set: export the password the stack was seeded with " +
        "(docs/testing/e2e.md). CI generates one per run.",
    );
  }
  return password;
}

// ---------------------------------------------------------------- scripts inside the API container

/** Runs a repository script with the API container's Python, settings and database. The image
 * ships no scripts/, so the file is piped in on stdin; stdout is returned, stderr only on failure. */
function runInApi(script: string, args: string[]): string {
  return execFileSync("docker", ["compose", "exec", "-T", "api", "python", "-", ...args], {
    cwd: COMPOSE_DIR,
    input: readFileSync(new URL(`../../../${script}`, import.meta.url)),
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 90_000,
  });
}

/** Opening balance for a test customer. There is no customer-facing deposit, so the money comes
 * from scripts/e2e_fund.py, which posts a ledger deposit (example.com customers only). */
export function fundCustomer(email: string, amountMinor: number): void {
  expect(runInApi("scripts/e2e_fund.py", [email, String(amountMinor)])).toContain("funded");
}

/** A staff TOTP code the API has not seen yet (replay protection allows each 30 s step once). */
export function staffCode(email: string): string {
  const code = /^(\d{6})$/m.exec(runInApi("scripts/totp.py", [email, "--unused"]))?.[1];
  if (!code) throw new Error(`no TOTP code for ${email} (was the stack seeded?)`);
  return code;
}

// ------------------------------------------------------------------------------------------- TOTP

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "234567"; // the RFC 4648 base32 alphabet
const STEP_MS = 30_000;

function base32Decode(text: string): Buffer {
  let bits = "";
  for (const char of text.replace(/[\s=]/g, "").toUpperCase()) {
    const value = BASE32.indexOf(char);
    if (value < 0) throw new Error("the key is not base32");
    bits += value.toString(2).padStart(5, "0");
  }
  const bytes = bits.match(/.{8}/g) ?? [];
  return Buffer.from(bytes.map((byte) => parseInt(byte, 2)));
}

/** RFC 6238 TOTP (SHA-1, 6 digits, 30 s), as pyotp and authenticator apps compute it. */
export function totpAt(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = (digest.at(-1) ?? 0) & 0x0f;
  const binary = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(binary % 1_000_000).padStart(6, "0");
}

/** The customer's authenticator app. The API accepts the previous, current or next step, each only
 * once, so every code is for a step after the last one used: the current step, else the next one.
 * Journeys use at most two codes per customer, so a code never has to wait for the clock. */
export class Authenticator {
  private lastStep = -1;

  constructor(readonly secret: string) {}

  code(): string {
    const current = Math.floor(Date.now() / STEP_MS);
    const step = Math.max(current, this.lastStep + 1);
    if (step > current + 1) throw new Error("more than two codes in one 30-second window");
    this.lastStep = step;
    return totpAt(this.secret, step);
  }
}

// ---------------------------------------------------------------------------------- customers

export interface Customer {
  email: string;
  password: string;
  fullName: string;
}

/** A new customer through the public API (the UI registration is journey 1's subject). */
export async function registerCustomer(
  request: APIRequestContext,
  label: string,
): Promise<Customer> {
  const tag = uniqueTag();
  const customer = {
    email: `e2e-${label}-${tag}@example.com`,
    password: newPassword(),
    fullName: `E2E ${label} ${tag}`,
  };
  const response = await request.post("/api/v1/auth/register", {
    data: { email: customer.email, password: customer.password, full_name: customer.fullName },
  });
  expect(response.status()).toBe(201);
  return customer;
}

export async function apiToken(request: APIRequestContext, who: Customer): Promise<string> {
  const response = await request.post("/api/v1/auth/login", {
    data: { email: who.email, password: who.password },
  });
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { access_token?: string };
  expect(body.access_token).toBeTruthy();
  return body.access_token ?? "";
}

// ------------------------------------------------------------------------------------ UI steps

export async function signIn(page: Page, email: string, password: string, path = "/") {
  await page.goto(path);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

export async function enterSignInCode(page: Page, code: string) {
  await page.getByLabel("Authenticator or recovery code").fill(code);
  await page.getByRole("button", { name: "Verify" }).click();
}

/** Profile & security -> set up the authenticator: the test reads the key off the page, as a
 * person would type it into an app, and confirms with the first code. */
export async function enrolAuthenticator(page: Page): Promise<Authenticator> {
  await page.getByRole("link", { name: "Profile & security" }).click();
  await page.getByRole("button", { name: "Set up authenticator app" }).click();
  const key = await page.getByRole("main").locator("code.secret").textContent();
  const app = new Authenticator((key ?? "").replace(/\s/g, ""));
  await page.getByLabel("Code from the app").fill(app.code());
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Authenticator app set up.")).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Recovery codes" }).getByRole("listitem"),
  ).not.toHaveCount(0);
  return app;
}

/** Adds a payee at another bank (verified without a name check) and returns its name. A payee
 * added seconds ago is "new" to the fraud rules, which journeys 2 and 3 rely on. */
export async function addPayee(page: Page, label: string): Promise<string> {
  const name = `Payee ${label} ${uniqueTag()}`;
  await page.getByRole("link", { name: "Payees" }).click();
  await page.getByRole("button", { name: "Add payee" }).click();
  await page.getByLabel("Payee name").fill(name);
  await page.getByLabel("Account number").fill(String(Date.now()).slice(-12));
  await page.getByLabel("Bank code").fill("EXTB0001");
  await page.getByRole("button", { name: "Add and check name" }).click();
  await expect(page.getByText("This account is at another bank")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: name })).toContainText("Verified");
  return name;
}

/** Minor units as the web app shows them (lib/money.ts formatMinor): 2500000 -> "INR 25,000.00". */
export function inr(minor: number): string {
  const major = new Intl.NumberFormat("en-IN").format(Math.trunc(minor / 100));
  return `INR ${major}.${String(minor % 100).padStart(2, "0")}`;
}

/** Accounts -> Send money -> review dialog. Leaves the "Check and confirm" dialog open. */
export async function reviewTransfer(
  page: Page,
  payee: string,
  amountMinor: number,
  reference: string,
) {
  await page.getByRole("link", { name: "Accounts" }).click();
  const payeeSelect = page.getByLabel("Payee", { exact: true });
  const option = payeeSelect.locator("option", { hasText: payee });
  await expect(option).toHaveCount(1);
  await payeeSelect.selectOption((await option.getAttribute("value")) ?? "");
  await page.getByLabel("Amount (INR)").fill((amountMinor / 100).toFixed(2));
  await page.getByLabel("Reference (optional)").fill(reference);
  await page.getByRole("button", { name: "Review transfer" }).click();
  const review = page.getByRole("dialog", { name: "Check and confirm" });
  await expect(review).toContainText(payee);
  await expect(review).toContainText(inr(amountMinor));
}

/** The first account's available balance and on-hold amount, as shown on Accounts. */
export function balances(page: Page) {
  const account = page.locator("li.account").first();
  return {
    available: account.getByLabel("available balance"),
    held: account.getByLabel("held funds"),
    ledger: account.getByLabel("ledger balance"),
    transactions: account.getByRole("button", { name: "Transactions" }),
    entries: account.getByRole("table", { name: "Transactions, newest first" }),
  };
}
