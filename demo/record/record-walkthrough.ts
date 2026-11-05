import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { expect, test } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";
import {
  Authenticator,
  inr,
  newPassword,
  reviewTransfer,
  staffCode,
} from "../../apps/web/e2e/support";
import { Walkthrough, demoPassword, grafanaPassword, pathOf } from "./walkthrough-kit";
import type { Outcome, Timeline } from "./walkthrough-kit";

// Item 14: the A-to-Z product walkthrough. One 1920x1080 recording of every sign-in, feature and
// dashboard, in the order of the owner's list, on the running stack. It writes the raw WebM and a
// timeline JSON (chapters, steps, captions, and the dead-time and 1.5x marks); edit-walkthrough.py
// cuts that into the 10-15 minute film, the 3-minute highlights, chapters.txt and the .srt.
// Every step checks the API's answer and the run stops at the first one the story does not allow.
// Steps for features that are not on this build yet are recorded as PENDING and take no video time.
// Passwords, codes, keys and tokens are never printed; the MFA key and recovery codes are blurred.

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../..");
const SIZE = { width: 1920, height: 1080 };
const PHONE = { width: 390, height: 844 };
const PACE = Number(process.env.DEMO_PACE ?? "1");
const CAPTIONS = process.env.DEMO_CAPTIONS !== "0";
const GRAFANA = process.env.DEMO_GRAFANA_URL ?? "http://localhost:3000";
const DAY_MS = 86_400_000;
const IST_OFFSET_MS = 5.5 * 3_600_000;

const ALICE = "alice@demo.pbip";
const BOB_NAME = "Bob Demo";
const ANALYST = "analyst@demo.pbip";
const COMPLIANCE = "compliance@demo.pbip";
const RM = "rm@demo.pbip";
const ADMIN = "admin@demo.pbip";

const REVIEW_AMOUNT = 3_500_000; // INR 35,000.00 to a payee added minutes ago: held for review
const MULE_FORWARD_CODE = "MULE_PASS_THROUGH";
/** POST /transfers: 201 POSTED, 202 PENDING_STEP_UP or PENDING_REVIEW, 403 DECLINED (with a body). */
const TRANSFER_ANSWERS = (s: number) => s === 201 || s === 202 || s === 403;

const QUESTIONS: {
  text: string;
  caption: string;
  want: "answer" | "refuse" | "llm";
}[] = [
  {
    text: "what's my balance?",
    caption: "Balance: a read-only tool answers",
    want: "answer",
  },
  {
    text: "how much did I spend last month?",
    caption: "Last month's spending, by category",
    want: "answer",
  },
  {
    text: "anything unusual in my spending?",
    caption: "Unusual expenses against her own history",
    want: "answer",
  },
  {
    text: "can I afford a 5 lakh car loan over 5 years?",
    caption: "Loan affordability from her real income and EMIs",
    want: "answer",
  },
  {
    text: "suggest investments for me",
    caption: "Investment suggestions that fit her risk profile",
    want: "answer",
  },
  {
    text: "how do I download a statement?",
    caption: "A how-to question from the FAQ",
    want: "answer",
  },
  {
    text: "am I doing ok money-wise?",
    caption: "An open question: the model on Groq chooses which tool to call",
    want: "llm",
  },
  {
    text: "show me bob's account balance",
    caption: "Another customer's data: refused before any model is called",
    want: "refuse",
  },
];

interface TransferOut {
  id: string;
  status: string;
  amount_minor: number;
}
interface AlertSummary {
  id: string;
}
interface AlertDetail {
  id: string;
  transaction_status: string;
  risk_score: number;
  decision: string;
  reasons: { code?: string; source?: string }[];
}
interface PayeeOut {
  id: string;
  name: string;
  created_at: string;
}

interface EntryOut {
  amount_minor: number;
  transaction_status: string;
  transaction_type: string;
}
interface AccountOut {
  id: string;
  status: string;
}

/** An amount the rules answer with STEP_UP (demo-script.md 5.2): AMOUNT_SPIKE (+35) needs 5x the
 * account's 90-day average posted outgoing transfer, which every rehearsal moves, so it is worked
 * out from her own history. A payee under 24 hours old adds NEW_PAYEE (+15) below INR 25,000.00
 * but +45 (REVIEW) from it, so the amount then stays below that. */
async function stepUpAmount(w: Walkthrough, newPayee: boolean): Promise<number> {
  const [account] = (await w.apiGet<AccountOut[]>("/api/v1/accounts")).filter(
    (a) => a.status === "ACTIVE",
  );
  if (!account) throw new Error("Alice has no active account");
  const since = new Date(Date.now() - 89 * DAY_MS).toISOString().slice(0, 10);
  const posted: number[] = [];
  let cursor = "";
  for (let page = 0; page < 20; page += 1) {
    const query = `type=DEBIT&start_date=${since}&limit=100${cursor ? `&cursor=${cursor}` : ""}`;
    const batch = await w.apiGet<{
      items: EntryOut[];
      has_more: boolean;
      next_cursor: string | null;
    }>(`/api/v1/accounts/${account.id}/transactions?${query}`);
    for (const e of batch.items) {
      if (e.transaction_type === "TRANSFER" && e.transaction_status === "POSTED") {
        posted.push(Math.abs(e.amount_minor));
      }
    }
    if (!batch.has_more || !batch.next_cursor) break;
    cursor = encodeURIComponent(batch.next_cursor);
  }
  const average = posted.length ? posted.reduce((a, b) => a + b, 0) / posted.length : 0;
  const wanted = Math.ceil((average * 5.05) / 100_000) * 100_000; // whole INR 1,000.00
  const cap = newPayee ? 2_490_000 : 9_900_000; // below NEW_PAYEE_LARGE_AMOUNT / LARGE_AMOUNT
  return Math.min(Math.max(wanted, 2_000_000), cap);
}

function tag(): string {
  return randomUUID().slice(0, 4);
}

function externalAccountNumber(): string {
  return String(BigInt(`0x${randomUUID().replace(/-/g, "").slice(0, 12)}`) % 10n ** 12n).padStart(
    12,
    "0",
  );
}

function istDate(offsetDays: number): string {
  return new Date(Date.now() + IST_OFFSET_MS + offsetDays * DAY_MS).toISOString().slice(0, 10);
}

function codes(alert: AlertDetail): string {
  return alert.reasons.map((r) => r.code ?? "?").join(",");
}

/** Whether something is on this build: a quick look, never a failure. */
async function present(locator: Locator): Promise<boolean> {
  try {
    await locator.first().waitFor({ state: "visible", timeout: 4_000 });
    return true;
  } catch {
    return false;
  }
}

test("PBIP A-to-Z walkthrough recording (item 14)", async ({ browser }) => {
  test.setTimeout(60 * 60_000);
  const outDir = resolve(process.env.DEMO_VIDEO_DIR ?? join(HERE, "out"));
  mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const rawDir = join(outDir, `.raw-${stamp}`);
  const context = await browser.newContext({
    viewport: SIZE,
    recordVideo: { dir: rawDir, size: SIZE },
    acceptDownloads: true,
  });
  await Walkthrough.prepare(context);
  const page = await context.newPage();
  const w = new Walkthrough(page, demoPassword(REPO_ROOT), PACE, CAPTIONS);
  let failed: unknown = null;
  try {
    await story(w, page, outDir);
  } catch (error) {
    failed = error;
  } finally {
    const video = page.video();
    await context.close();
    const name = `pbip-walkthrough-${stamp}${failed ? "-FAILED" : ""}`;
    const webm = join(outDir, `${name}.webm`);
    if (video) {
      await video.saveAs(webm);
      await video.delete();
      rmSync(rawDir, { recursive: true, force: true });
    }
    const timeline: Timeline = {
      version: 1,
      video: `${name}.webm`,
      size: SIZE,
      recorded_at: new Date(w.t0).toISOString(),
      chapters: w.chapters,
      steps: w.steps,
      captions: w.captions,
      sign_in_requests: w.signInRequests,
    };
    writeFileSync(join(outDir, `${name}.timeline.json`), `${JSON.stringify(timeline, null, 2)}\n`);
    const count = (o: Outcome) => w.steps.filter((s) => s.outcome === o).length;
    console.log(`\nVIDEO ${webm}\nTIMELINE ${join(outDir, `${name}.timeline.json`)}`);
    console.log(
      `SUMMARY PASS ${count("PASS")} DIFF ${count("DIFF")} PENDING ${count("PENDING")} FAIL ${count("FAIL")}`,
    );
    for (const s of w.steps.filter((x) => x.outcome === "PENDING"))
      console.log(`PENDING ${s.name}`);
    console.log(`SIGN-IN REQUESTS ${w.signInRequests}`);
  }
  if (failed) throw failed;
});

/** DEMO_PARTS (comma-separated) records only some parts, for rehearsing one chapter at a time.
 * The release recording runs them all. */
const PARTS = (process.env.DEMO_PARTS ?? "")
  .split(",")
  .map((p) => p.trim())
  .filter((p) => p !== "");
function wanted(part: string): boolean {
  return PARTS.length === 0 || PARTS.includes(part);
}

async function story(w: Walkthrough, page: Page, outDir: string): Promise<void> {
  if (!wanted("onboarding")) await w.dead(() => page.goto("/"));
  const newcomer = wanted("onboarding") ? await onboarding(w, page) : null;
  const held = wanted("customer") ? await customerBanking(w, page) : null;
  if (wanted("analyst")) await staffAnalyst(w, page, held);
  if (wanted("compliance")) await staffCompliance(w, page);
  if (wanted("rm")) await staffRelationshipManager(w, page);
  if (wanted("admin")) await staffAdmin(w, page, newcomer);
  if (wanted("model")) await modelAccuracy(w, page, outDir);
  if (wanted("grafana")) await grafana(w, page);
  await w.step("closing", {}, async () => {
    await w.say("End of the PBIP walkthrough", 2500);
    return ["PASS", "end"];
  });
}

// ======================================================================= 1. onboarding and MFA

interface Newcomer {
  email: string;
  password: string;
  userId: string;
  app: Authenticator;
}

async function onboarding(w: Walkthrough, page: Page): Promise<Newcomer> {
  const who: Newcomer = {
    email: `walkthrough-${tag()}${tag()}@example.com`,
    password: newPassword(),
    userId: "",
    app: new Authenticator("A"),
  };
  w.chapter("Registration and MFA enrolment");
  await w.step("customer registration", {}, async () => {
    await w.dead(() => page.goto("/"));
    await w.say("A new customer opens an account online", 1200);
    await page.getByRole("button", { name: "Create an account" }).click();
    await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
    await w.fast(async () => {
      await page.getByLabel("Full name").fill(`Priya Walkthrough ${tag()}`);
      await page.getByLabel("Email").fill(who.email);
      await page.getByLabel("Password", { exact: true }).fill(who.password);
      await page.getByLabel("Confirm password").fill(who.password);
      await w.hold(600);
    });
    const { status } = await w.api("POST", "/api/v1/auth/register", () =>
      page.getByRole("button", { name: "Create account" }).click(),
    );
    await w.dead(() => expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible());
    const me = await w.apiGet<{ id: string }>("/api/v1/auth/me");
    who.userId = me.id;
    await w.say("Registered and signed in: a savings account is opened automatically", 2500);
    return ["PASS", `register ${status}; savings account opened`];
  });

  await w.step("MFA enrolment (key and recovery codes blurred)", { highlight: false }, async () => {
    await w.go("/profile", "Profile & security");
    await w.reveal(page.getByRole("heading", { name: "Two-step sign-in" }));
    await w.say("She turns on two-step sign-in with an authenticator app", 1000);
    await w.api("POST", "/api/v1/auth/mfa/enroll", () =>
      page.getByRole("button", { name: "Set up authenticator app" }).click(),
    );
    const key = await page.getByRole("main").locator("code.secret").textContent();
    who.app = new Authenticator((key ?? "").replace(/\s/g, ""));
    await w.say("The key is blurred for this recording; it is typed into the app", 2500);
    await page.getByLabel("Code from the app").fill(who.app.code());
    await w.hold(500);
    await w.api("POST", "/api/v1/auth/mfa/confirm", () =>
      page.getByRole("button", { name: "Confirm" }).click(),
    );
    await expect(page.getByText("Authenticator app set up.")).toBeVisible();
    const recovery = page.getByRole("list", { name: "Recovery codes" }).getByRole("listitem");
    await expect(recovery).toHaveCount(8);
    await w.say("Eight one-time recovery codes, shown once (blurred here)", 3000);
    return ["PASS", "enrolled; 8 recovery codes"];
  });

  w.chapter("Signing in with MFA", true);
  await w.step(
    "sign-in: password, a wrong code is refused, then the right code",
    { highlight: true },
    async () => {
      await w.signOut();
      await w.signInBudget(3);
      await w.say("Signing in: password first, then the authenticator code", 600);
      await w.dead(() => page.goto("/"));
      await w.fast(async () => {
        await page.getByLabel("Email").fill(who.email);
        await page.getByLabel("Password").fill(who.password);
      });
      await w.api("POST", "/api/v1/auth/login", () =>
        page.getByRole("button", { name: "Sign in" }).click(),
      );
      const codeBox = page.getByLabel("Authenticator or recovery code");
      await expect(codeBox).toBeVisible();
      const good = who.app.code();
      const wrong = String((Number(good) + 500_000) % 1_000_000).padStart(6, "0");
      await codeBox.fill(wrong);
      await w.say("A wrong code is refused", 400);
      await w.api(
        "POST",
        "/api/v1/auth/mfa/verify",
        () => page.getByRole("button", { name: "Verify" }).click(),
        (s) => s === 400 || s === 401,
      );
      await expect(page.getByRole("alert").first()).toBeVisible();
      await w.hold(2200);
      await codeBox.fill(good);
      await w.say("The right code: signed in", 300);
      await w.api("POST", "/api/v1/auth/mfa/verify", () =>
        page.getByRole("button", { name: "Verify" }).click(),
      );
      await w.dead(() => expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible());
      await w.hold(1800);
      return ["PASS", "wrong code refused; right code accepted"];
    },
  );
  await w.signOut();
  return who;
}

// ===================================================================== 2. customer banking (Alice)

async function customerBanking(w: Walkthrough, page: Page): Promise<TransferOut | null> {
  w.chapter("Accounts and transactions");
  await w.step(
    "alice signs in with password and authenticator code",
    { highlight: true },
    async () => {
      await w.say("Alice, an existing customer, signs in with her password and code", 300);
      await w.signInWithCode(ALICE, "/", "My accounts");
      await w.hold(800);
      return ["PASS", "password + TOTP accepted"];
    },
  );

  await w.step("accounts and total balance", {}, async () => {
    const summary = await w.apiGet<{ totals?: unknown[] }>("/api/v1/accounts/summary");
    await expect(page.getByRole("heading", { name: "Total balance" })).toBeVisible();
    await w.say("Total balance across her accounts: available, ledger and on hold", 3500);
    return ["PASS", `summary totals=${(summary.totals ?? []).length}`];
  });

  await w.step("transaction search and filters", {}, async () => {
    await w.say("Each account's history, with search and filters", 300);
    const account = page.locator("li.account").first();
    await w.api("GET", /^\/api\/v1\/accounts\/[^/]+\/transactions$/, () =>
      account.getByRole("button", { name: "Transactions" }).click(),
    );
    const table = account.getByRole("table", {
      name: "Transactions, newest first",
    });
    await expect(table).toBeVisible();
    await w.reveal(account.getByRole("form", { name: "Search transactions" }));
    await w.hold(1200);
    await page.getByLabel("Search references").fill("Salary");
    const filtered = await w.api<{ items?: unknown[] } | unknown[]>(
      "GET",
      /^\/api\/v1\/accounts\/[^/]+\/transactions$/,
      () => page.getByRole("button", { name: "Apply filters" }).click(),
    );
    await expect(table.getByRole("row").filter({ hasText: "Salary" }).first()).toBeVisible();
    await w.say("Search 'Salary': only her salary credits", 2500);
    await page.getByLabel("Money in or out").selectOption({ index: 2 });
    await page.getByLabel("Search references").fill("");
    await w.api("GET", /^\/api\/v1\/accounts\/[^/]+\/transactions$/, () =>
      page.getByRole("button", { name: "Apply filters" }).click(),
    );
    await w.say("Money out only", 2200);
    const rows = Array.isArray(filtered.body)
      ? filtered.body.length
      : (filtered.body.items ?? []).length;
    return ["PASS", `salary search rows=${rows}`];
  });

  await w.step("statement PDF download", {}, async () => {
    await w.go("/statements", "Statements");
    await w.say("Statements: any period up to a year, as PDF or CSV", 1200);
    await page.getByLabel("PDF: a formatted statement to read or print").check();
    const download = page.waitForEvent("download");
    const { status } = await w.api("GET", /^\/api\/v1\/accounts\/[^/]+\/statements$/, () =>
      page.getByRole("button", { name: "Download statement" }).click(),
    );
    const file = (await w.dead(() => download)).suggestedFilename();
    if (!file.endsWith(".pdf")) throw new Error(`downloaded ${file}, not a PDF`);
    await expect(page.getByText(`Downloaded ${file}`)).toBeVisible();
    await w.say("Downloaded the PDF statement (and a security notification is sent)", 2500);
    return ["PASS", `statement ${status} ${file}`];
  });

  w.chapter("Payees and payments", true);
  const payee = `Asha Traders ${tag()}`;
  await w.step("payees: add, edit and verify", {}, async () => {
    await w.go("/payees", "Payees");
    await w.say("Payees: add one, with a name check (Confirmation of Payee)", 300);
    await page.getByRole("button", { name: "Add payee" }).click();
    await w.fast(async () => {
      await page.getByLabel("Payee name").fill(`${payee} Ltd`);
      await page.getByLabel("Account number").fill(externalAccountNumber());
      await page.getByLabel("Bank code").fill("EXTB0001");
    });
    await w.api("POST", "/api/v1/payees", () =>
      page.getByRole("button", { name: "Add and check name" }).click(),
    );
    await expect(page.getByText("This account is at another bank")).toBeVisible();
    await w.hold(1800);
    const row = page.getByRole("row").filter({ hasText: payee });
    await w.say("Edit the name: the payee must be verified again", 300);
    await row.getByRole("button", { name: "Edit" }).click();
    const dialog = page.getByRole("dialog", { name: "Edit payee" });
    await dialog.getByLabel("Payee name").fill(payee);
    await w.api("PATCH", /^\/api\/v1\/payees\/[^/]+$/, () =>
      dialog.getByRole("button", { name: "Save" }).click(),
    );
    await expect(row).toContainText("Not verified");
    await w.hold(1500);
    await w.say("Verify runs the name check again", 300);
    await w.api("POST", /^\/api\/v1\/payees\/[^/]+\/verify$/, () =>
      row.getByRole("button", { name: "Verify" }).click(),
    );
    await expect(row).toContainText("Verified");
    await w.hold(2000);
    return ["PASS", "added, edited (not verified), verified"];
  });

  let held: TransferOut | null = null;
  await w.step("transfer to a new payee: review, held for review", {}, async () => {
    await w.say(`Send money: ${inr(REVIEW_AMOUNT)} to the payee she added a minute ago`, 300);
    await w.dead(() => w.go("/accounts", "My accounts"));
    await w.fast(() => reviewTransfer(page, payee, REVIEW_AMOUNT, `Invoice ${tag()}`));
    await w.say("Review: check the details before sending", 1800);
    const { body: t } = await w.api<TransferOut>(
      "POST",
      "/api/v1/transfers",
      () => page.getByRole("button", { name: "Send money" }).click(),
      TRANSFER_ANSWERS,
    );
    if (t.status === "PENDING_REVIEW") {
      held = t;
      await expect(page.getByRole("status").filter({ hasText: "being reviewed" })).toBeVisible();
      await w.say(
        "Held for review: the money is on hold. She is never told the score or reasons",
        3500,
      );
      return ["PASS", `status=${t.status}`];
    }
    if (t.status === "DECLINED") {
      held = t; // a blocked payment opens an alert too; the analyst labels it
      await w.say("Declined: the top risk band. No money moved", 3000);
      return ["DIFF", "status=DECLINED (BLOCK, demo-script section 7)"];
    }
    throw new Error(`expected PENDING_REVIEW, got ${t.status}`);
  });

  await w.step("transfer with a step-up code and a receipt", { highlight: true }, async () => {
    const payees = await w.apiGet<PayeeOut[]>("/api/v1/payees");
    const bob = payees.find((p) => p.name === BOB_NAME);
    if (!bob) throw new Error("Alice has no payee named Bob Demo");
    const ageMs = Date.now() - Date.parse(bob.created_at);
    const amount = await stepUpAmount(w, ageMs < DAY_MS);
    await w.say(`${inr(amount)} to Bob Demo: unusually large for her`, 300);
    await w.fast(() => reviewTransfer(page, BOB_NAME, amount, `Rent share ${tag()}`));
    await w.hold(900);
    const { body: t } = await w.api<TransferOut>(
      "POST",
      "/api/v1/transfers",
      () => page.getByRole("button", { name: "Send money" }).click(),
      TRANSFER_ANSWERS,
    );
    if (t.status === "PENDING_REVIEW") {
      await w.say("Held for review instead of a step-up (documented alternative)", 3000);
      return ["DIFF", `amount=${inr(amount)} status=PENDING_REVIEW (demo-script 5.2)`];
    }
    if (t.status === "POSTED") {
      await w.say("Posted at once: the engine saw nothing unusual", 2500);
      return ["DIFF", `amount=${inr(amount)} POSTED without a step-up`];
    }
    if (t.status !== "PENDING_STEP_UP")
      throw new Error(`expected PENDING_STEP_UP, got ${t.status}`);
    const dialog = page.getByRole("dialog", { name: "Confirm it's you" });
    await expect(dialog).toContainText(inr(amount));
    await w.say("Step-up: the bank asks for her authenticator code before any money moves", 2500);
    const code = await w.dead(() => Promise.resolve(staffCode(ALICE)));
    await dialog.getByLabel("Authenticator code").fill(code);
    const { body: after } = await w.api<TransferOut>("POST", /\/step-up$/, () =>
      dialog.getByRole("button", { name: "Confirm transfer" }).click(),
    );
    if (after.status !== "POSTED") throw new Error(`step-up gave ${after.status}`);
    await expect(page.getByRole("status").filter({ hasText: "Sent" })).toContainText(inr(amount));
    await w.say("Code accepted: posted to the ledger. This message is her receipt", 3500);
    return ["PASS", `amount=${inr(amount)} PENDING_STEP_UP -> ${after.status}`];
  });

  await w.step("bill payment", {}, async () => {
    await w.go("/bills", "Pay bills");
    await w.say("Pay bills: electricity, water, mobile, gas, broadband, insurance", 300);
    await w.fast(async () => {
      await page.getByLabel("Biller").selectOption({ index: 1 });
      await page.getByLabel("Consumer number").fill(`CN${tag()}${tag()}`);
      await page.getByLabel("Amount (INR)").fill("1250.00");
    });
    await page.getByRole("button", { name: "Review payment" }).click();
    const dialog = page.getByRole("dialog", { name: "Check and confirm" });
    await expect(dialog).toBeVisible();
    await w.hold(1200);
    const { body } = await w.api<{ status: string }>("POST", "/api/v1/bills/pay", () =>
      dialog.getByRole("button", { name: "Pay bill" }).click(),
    );
    if (body.status === "POSTED") {
      await expect(page.getByRole("heading", { name: "Bill paid" })).toBeVisible();
      await w.say("Bill paid: a receipt with the clearing reference", 2800);
      return ["PASS", "status=POSTED"];
    }
    if (body.status === "PENDING_REVIEW") {
      await w.say("Held for review: bill payments never ask for a code", 2800);
      return ["DIFF", "status=PENDING_REVIEW"];
    }
    throw new Error(`bill payment ${body.status}`);
  });

  await w.step("scheduled transfer", {}, async () => {
    await w.go("/scheduled", "Scheduled transfers");
    await w.say("Scheduled transfers: standing orders, checked on the day like any payment", 300);
    await page.getByRole("button", { name: "Schedule a transfer" }).click();
    const form = page.getByRole("dialog", { name: "Schedule a transfer" });
    await w.fast(async () => {
      const payTo = form.getByLabel("Pay to");
      const option = payTo.locator("option", { hasText: BOB_NAME });
      await payTo.selectOption((await option.first().getAttribute("value")) ?? "");
      await form.getByLabel("Amount (INR)").fill("1500.00");
      await form.getByLabel("How often").selectOption({ label: "Every month" });
      await form.getByLabel("First payment").fill(istDate(7));
      await form.getByLabel("Reference (optional)").fill(`Gym ${tag()}`);
    });
    await w.hold(800);
    const { status } = await w.api("POST", "/api/v1/transfers/scheduled", () =>
      form.getByRole("button", { name: "Schedule transfer" }).click(),
    );
    await expect(
      page.getByRole("status").filter({ hasText: "The first payment is on" }),
    ).toBeVisible();
    await w.say("Scheduled monthly. It can be paused, resumed or cancelled", 2500);
    return ["PASS", `scheduled ${status}`];
  });

  w.chapter("Notifications and insights");
  await w.step("notifications", {}, async () => {
    await w.say("Notifications: every payment and security event", 300);
    const bell = page.getByRole("button", { name: /^Notifications \(/ });
    await bell.click();
    const tray = page.getByRole("dialog", { name: "Notifications tray" });
    await expect(tray).toBeVisible();
    await w.hold(3000);
    const mark = tray.getByRole("button", { name: "Mark all as read" });
    if (await mark.isVisible()) {
      await w.api("POST", /^\/api\/v1\/notifications/, () => mark.click());
      await w.hold(1200);
    }
    await page.keyboard.press("Escape");
    return ["PASS", "tray opened, all marked read"];
  });

  await w.step("spending and financial health", {}, async () => {
    const { body } = await w.api<{ overall_score?: number }>(
      "GET",
      "/api/v1/analytics/health-score",
      () => w.go("/insights", "Spending & Financial Health"),
    );
    await expect(page.getByRole("heading", { name: "Financial Health Score" })).toBeVisible();
    await w.say(
      `Financial Health Score ${body.overall_score ?? "?"} of 100, from four explained parts`,
      3200,
    );
    await w.reveal(page.getByRole("heading", { name: "Spending by Category" }));
    await w.say("Spending by category, and month-over-month trends", 2600);
    await w.reveal(page.getByRole("heading", { name: "Month-over-Month Trends" }));
    await w.hold(1800);
    return ["PASS", `health score=${body.overall_score ?? "?"}`];
  });

  await w.step("balance forecast", {}, async () => {
    await w.reveal(page.getByRole("heading", { name: "Balance Forecast" }));
    await expect(
      page
        .getByRole("img", { name: /forecast chart/ })
        .or(page.getByLabel(/forecast chart/))
        .first(),
    ).toBeVisible();
    await w.say(
      "Balance forecast: 30 days ahead with 80% and 95% ranges, scheduled payments marked",
      3500,
    );
    return ["PASS", "forecast chart shown"];
  });

  w.chapter("Loans");
  await w.step("loans: eligibility, EMI, application, repayment schedule", {}, async () => {
    await w.go("/loans", "Loans");
    const offers = page.getByRole("heading", { name: "Loans for you" });
    if (await w.dead(() => present(offers))) {
      await w.reveal(offers);
      await w.say("Loans for you: recommended from her balance and spending", 3000);
    } else {
      w.pending("loan recommendations (Loans for you)", "no 'Loans for you' section on this build");
    }
    await w.reveal(page.getByRole("heading", { name: "Apply for a loan" }));
    await w.say("Pick a product, amount and tenure: the EMI is worked out live", 300);
    const product = page.getByLabel("Loan product");
    await product.selectOption({ index: 0 });
    const amount = page.getByLabel("Loan amount");
    const min = Number((await amount.getAttribute("min")) ?? "0");
    const max = Number((await amount.getAttribute("max")) ?? "0");
    const step = Number((await amount.getAttribute("step")) ?? "1") || 1;
    const target = Math.round((min + (max - min) * 0.1) / step) * step;
    await amount.fill(String(target));
    await w.dead(() => expect(page.getByRole("region", { name: "EMI estimate" })).toBeVisible());
    await w.hold(2000);
    const eligibility = page.getByRole("heading", {
      name: "Eligibility pre-check",
    });
    await w.reveal(eligibility);
    await w.dead(() => expect(page.getByText(/^(Eligible|Not eligible)$/).first()).toBeVisible());
    const eligible = await page.getByText("Eligible", { exact: true }).first().isVisible();
    await w.say(
      eligible
        ? "Eligibility pre-check: all EMIs stay within 50% of her regular income"
        : "Eligibility pre-check: not eligible, with the reasons",
      3000,
    );
    if (!eligible) return ["DIFF", "not eligible on this data; no application"];
    await page.getByRole("button", { name: "Apply for this loan" }).click();
    const confirm = page.getByRole("dialog", {
      name: "Check and confirm your loan",
    });
    await expect(confirm).toBeVisible();
    await w.hold(1500);
    const { body } = await w.api<{ status?: string; id?: string }>(
      "POST",
      "/api/v1/loans/apply",
      () => confirm.getByRole("button", { name: "Confirm and receive the money" }).click(),
    );
    await expect(page.getByRole("heading", { name: "Loan approved and disbursed" })).toBeVisible();
    await w.say("Approved and disbursed into her account at once", 2500);
    const close = page.getByRole("button", { name: "Done" });
    if (await close.isVisible()) await close.click();
    await w.reveal(page.getByRole("heading", { name: "Your loans" }));
    await w.api("GET", /^\/api\/v1\/loans\/[^/]+$/, () =>
      page.getByRole("button", { name: "Show upcoming EMIs" }).first().click(),
    );
    await w.say("Her repayment schedule: the next EMIs and their status", 3000);
    return ["PASS", `loan status=${body.status ?? "?"}`];
  });

  w.chapter("Wealth, investing and insurance");
  await w.step("fixed deposit booking", {}, async () => {
    await w.go("/wealth", "Wealth");
    await w.say("Wealth: her portfolio, fixed deposits and the product catalogue", 2500);
    const book = page.getByRole("button", { name: "Book this deposit" }).first();
    await w.reveal(book);
    await book.click();
    const dialog = page.getByRole("dialog").filter({ hasText: "Book:" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Amount").fill("25000.00");
    await w.dead(() =>
      expect(dialog.getByRole("region", { name: "Maturity preview" })).toBeVisible(),
    );
    await w.say("A live maturity preview: rate, maturity date and interest", 2500);
    const { status } = await w.api("POST", "/api/v1/wealth/fixed-deposits/book", () =>
      dialog.getByRole("button", { name: "Confirm and book" }).click(),
    );
    await expect(page.getByRole("region", { name: "Booking receipt" })).toBeVisible();
    await w.say("Fixed deposit booked, with a reference", 2500);
    await page.getByRole("button", { name: "Done" }).click();
    return ["PASS", `booked ${status}`];
  });

  await investing(w, page);

  await w.step("portfolio", {}, async () => {
    await w.reveal(page.getByRole("heading", { name: "Portfolio" }));
    await expect(page.getByRole("region", { name: "Portfolio summary" })).toBeVisible();
    await w.say("Portfolio: invested capital, returns and the asset allocation", 3000);
    return ["PASS", "portfolio shown"];
  });

  await insuranceQuote(w, page);

  await w.step("product comparison", {}, async () => {
    await w.go("/wealth/compare", "Compare products");
    await w.say("Compare up to four products side by side, insurance included", 300);
    await page
      .getByRole("checkbox", {
        name: "Fixed deposit, 1 to 3 years",
        exact: true,
      })
      .check();
    await page
      .getByRole("checkbox", {
        name: "Term life cover (illustrative)",
        exact: true,
      })
      .check();
    const table = page.getByRole("table", { name: "Product comparison" });
    await expect(table).toBeVisible();
    await w.reveal(table);
    if (await table.getByText("Suits your risk band").count()) {
      await w.say("Including whether each product suits her risk band", 3000);
    } else {
      await w.hold(3000);
    }
    return ["PASS", "2 products compared"];
  });

  await w.step("recommendations with reasons", {}, async () => {
    await w.go("/accounts", "My accounts");
    const heading = page.getByRole("heading", { name: "Recommended for you" });
    await w.reveal(heading);
    const why = page.getByRole("button", {
      name: "Why this was recommended for you",
    });
    if ((await why.count()) === 0) {
      await w.say("No product recommendations for her right now", 2000);
      return ["DIFF", "no recommendations on this data"];
    }
    await why.first().click();
    await w.say("Recommendations always say why: the reasons come from her own data", 3500);
    return ["PASS", `${await why.count()} recommendations`];
  });

  w.chapter("The PBIP assistant on Groq", true);
  await w.step("assistant questions", { highlight: true }, async () => {
    await w.say("Ask PBIP: read-only tools, answers worded by a model on Groq", 300);
    await page.getByRole("button", { name: "Ask the PBIP assistant" }).click();
    const drawer = page.getByRole("dialog", { name: "PBIP assistant" });
    await expect(drawer).toBeVisible();
    const engines: string[] = [];
    for (const q of QUESTIONS) {
      await w.say(q.caption, 200);
      const before = await drawer.locator(".assistant-msg--assistant").count();
      await drawer.getByRole("textbox", { name: "Message" }).fill(q.text);
      const { body } = await w.api<string>("POST", /^\/api\/v1\/assistant\/chat/, () =>
        drawer.getByRole("button", { name: "Send" }).click(),
      );
      const reply = drawer.locator(".assistant-msg--assistant").nth(before);
      await w.dead(async () => {
        await expect(reply).not.toHaveAttribute("aria-busy", "true", {
          timeout: 45_000,
        });
        await expect(drawer.getByRole("textbox", { name: "Message" })).toBeEnabled();
      });
      const text = ((await reply.textContent()) ?? "").trim();
      const engine =
        /"engine":\s*"(\w+)"/.exec(typeof body === "string" ? body : JSON.stringify(body))?.[1] ??
        "?";
      engines.push(engine);
      if (text === "") throw new Error(`empty reply to "${q.text}"`);
      if (q.want === "refuse" && !text.includes("only help with your own")) {
        throw new Error(`"${q.text}" was not refused`);
      }
      await w.hold(3200);
    }
    await drawer.getByRole("button", { name: "Close assistant" }).click();
    const llm = engines.filter((e) => e === "llm").length;
    return [llm > 0 ? "PASS" : "DIFF", `engines=${engines.join(",")} (llm=${llm})`];
  });

  w.chapter("Profile, security and preferences");
  await w.step("profile and security", {}, async () => {
    await w.go("/profile", "Profile & security");
    await w.say("Profile and security: details, two-step sign-in, devices, password", 2500);
    await w.reveal(page.getByRole("heading", { name: "Your devices" }));
    await w.hold(2000);
    return ["PASS", "profile shown"];
  });

  await w.step("risk profile", {}, async () => {
    const heading = page.getByRole("heading", { name: "Risk profile" });
    await w.reveal(heading);
    await w.say("Risk profile: five questions decide her risk band", 300);
    const section = page.locator("section").filter({ has: heading });
    const groups = section.locator("fieldset.choice-group");
    const n = await groups.count();
    await w.fast(async () => {
      for (let i = 0; i < n; i += 1) await groups.nth(i).getByRole("radio").nth(1).check();
    });
    const { body } = await w.api<{ band?: string }>("PUT", /risk-profile/, () =>
      section.getByRole("button", { name: "Save my answers" }).click(),
    );
    await expect(section.getByText(/Saved\. Your risk band is/)).toBeVisible();
    await w.say(`Saved: risk band ${String(body.band ?? "?").toLowerCase()}`, 2200);
    return ["PASS", `band=${body.band ?? "?"}`];
  });

  await w.step("consent: recommendations and analytics", {}, async () => {
    const dob = page.getByRole("heading", {
      name: "Date of birth and recommendations consent",
    });
    await w.reveal(dob);
    await w.say("Consent: she decides whether her age band is used for recommendations", 2800);
    const analytics = page.getByRole("heading", {
      name: "App usage analytics",
    });
    if (await w.dead(() => present(analytics))) {
      await w.reveal(analytics);
      await w.say("And whether her app usage feeds the bank's analytics", 2800);
      return ["PASS", "recommendations and analytics consent shown"];
    }
    w.pending("analytics consent", "no 'App usage analytics' section on this build");
    return ["PASS", "recommendations consent shown (analytics consent pending)"];
  });

  await w.step("light and dark theme", {}, async () => {
    await w.dead(() => page.evaluate(() => window.scrollTo(0, 0)));
    await w.say("Light and dark themes", 300);
    await page.getByRole("button", { name: "Switch to dark theme" }).click();
    await w.hold(2200);
    await page.getByRole("button", { name: "Switch to light theme" }).click();
    await w.hold(800);
    return ["PASS", "toggled dark and back"];
  });

  // The phone segment: the editor crops the 390x844 corner and centres it. The viewport changes
  // happen outside the step, so no full-size frame is inside it.
  await page.setViewportSize(PHONE);
  await w.step("phone layout (390x844)", { frame: PHONE }, async () => {
    await w.dead(() => w.go("/accounts", "My accounts"));
    await w.say("On a phone: one column, the menu folds away", 3000);
    await page.getByRole("button", { name: "Menu" }).click();
    await w.hold(3000);
    await page.getByRole("button", { name: "Menu" }).click();
    await w.hold(1200);
    return ["PASS", "390x844 segment"];
  });
  await page.setViewportSize(SIZE);

  await w.step("a customer is refused from a staff page", {}, async () => {
    await w.say("A customer opening a staff page gets 'not found', and the API refuses her", 300);
    await w.go("/staff/fraud", "Page not found");
    const response = await page.request.get("/api/v1/fraud/alerts", {
      headers: { Authorization: w.bearer },
    });
    if (response.status() !== 403)
      throw new Error(`fraud API answered ${response.status()} to a customer`);
    await w.hold(3000);
    return ["PASS", "page not found; GET /fraud/alerts 403"];
  });
  await w.signOut();
  return held;
}

/** The first INR amount in a field's hint ("At least INR 500.00", "INR 1,00,000.00 to ..."). */
async function hintAmount(page: Page, label: string): Promise<number> {
  const describedBy = await page
    .getByLabel(label, { exact: true })
    .getAttribute("aria-describedby");
  const hintId = describedBy?.split(" ")[0];
  if (!hintId) throw new Error(`the ${label} field has no hint`);
  const hint = (await page.locator(`[id="${hintId}"]`).textContent()) ?? "";
  const match = /INR ([\d,]+\.\d\d)/.exec(hint);
  if (!match?.[1]) throw new Error(`no amount in the ${label} hint`);
  return Number(match[1].replace(/,/g, ""));
}

/** Picks the first option of a select whose text matches. Returns false when there is none. */
async function pick(page: Page, label: string, text: RegExp): Promise<boolean> {
  const select = page.getByLabel(label, { exact: true });
  const options = await select.locator("option").allTextContents();
  const index = options.findIndex((o) => text.test(o));
  if (index < 0) return false;
  await select.selectOption({ index });
  return true;
}

async function investing(w: Walkthrough, page: Page): Promise<void> {
  await w.step("investing: a fund, a monthly SIP and a stock", {}, async () => {
    await w.go("/wealth/invest", "Invest");
    await w.say(
      "Invest: funds, stocks and bonds at today's simulated price, through the ledger",
      300,
    );
    if (!(await pick(page, "Investment", /Mutual fund$/))) throw new Error("no fund to buy");
    const min = await hintAmount(page, "Amount");
    await page.getByLabel("Amount", { exact: true }).fill(Math.max(min, 5000).toFixed(2));
    await page.getByRole("button", { name: "Review order" }).click();
    const confirm = page.getByRole("dialog", { name: "Confirm your order" });
    await expect(confirm).toBeVisible();
    await w.hold(1500);
    await w.api("POST", "/api/v1/wealth/investments/buy", () =>
      confirm.getByRole("button", { name: "Buy now" }).click(),
    );
    await expect(page.getByRole("heading", { name: "Order executed" })).toBeVisible();
    await w.say("Bought at today's price: units, price and a reference", 2500);
    const sip = page.getByRole("heading", { name: "Start a monthly SIP" });
    await w.reveal(sip);
    await w.say("Or invest every month: a SIP", 300);
    const sipMin = await hintAmount(page, "Monthly amount").catch(() => 1000);
    await page
      .getByLabel("Monthly amount", { exact: true })
      .fill(Math.max(sipMin, 2000).toFixed(2));
    await page.getByLabel("Day of the month", { exact: true }).fill("5");
    await page.getByRole("button", { name: "Review SIP" }).click();
    const sipConfirm = page.getByRole("dialog", { name: "Confirm your monthly SIP" });
    await expect(sipConfirm).toBeVisible();
    await w.hold(1200);
    await w.api("POST", "/api/v1/wealth/sips", () =>
      sipConfirm.getByRole("button", { name: "Start SIP" }).click(),
    );
    await w.hold(2000);
    await w.dead(() => page.evaluate(() => window.scrollTo(0, 0)));
    let stock = "";
    if (await pick(page, "Investment", /(Stock|Bond)$/)) {
      stock =
        (
          (await page
            .getByLabel("Investment", { exact: true })
            .locator("option:checked")
            .textContent()) ?? ""
        ).split(" · ")[0] ?? "";
      await w.say("Stocks and bonds are bought in whole units", 300);
      await page.getByLabel("Units", { exact: true }).fill("2");
      await page.getByRole("button", { name: "Review order" }).click();
      const second = page.getByRole("dialog", { name: "Confirm your order" });
      await w.hold(1000);
      await w.api("POST", "/api/v1/wealth/investments/buy", () =>
        second.getByRole("button", { name: "Buy now" }).click(),
      );
      await expect(page.getByRole("heading", { name: "Order executed" })).toBeVisible();
      await w.hold(2000);
    }
    sold.name = stock;
    return ["PASS", `fund bought, SIP started${stock ? `, 2 units of ${stock}` : ""}`];
  });

  await w.step("holdings: sell, and the SIP list", {}, async () => {
    await w.go("/wealth", "Wealth");
    const holdings = page.getByRole("heading", { name: "Your funds, stocks and bonds" });
    await w.reveal(holdings);
    await w.say("Her holdings, with today's value and gain or loss", 2500);
    let detail = "holdings shown";
    if (sold.name) {
      const units = page.getByLabel(`Units of ${sold.name} to sell`);
      await units.first().fill("1");
      const row = page.locator("li, tr, section").filter({ has: units.first() }).last();
      await row.getByRole("button", { name: "Sell", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: `Sell ${sold.name}` });
      await expect(dialog).toBeVisible();
      await w.hold(1200);
      await w.api("POST", /^\/api\/v1\/wealth\/holdings\/[^/]+\/sell$/, () =>
        dialog.getByRole("button", { name: "Sell now" }).click(),
      );
      await w.say("Sold one unit back into her account", 2200);
      detail = `sold 1 unit of ${sold.name}`;
    }
    await w.reveal(page.getByRole("heading", { name: "Your SIPs" }));
    await expect(page.getByRole("button", { name: "Cancel SIP" }).first()).toBeVisible();
    await w.say("Her SIPs: each can be cancelled; every monthly run is listed", 2500);
    return ["PASS", detail];
  });
}

const sold = { name: "" };

async function insuranceQuote(w: Walkthrough, page: Page): Promise<void> {
  await w.step("insurance quote and application", {}, async () => {
    await w.go("/wealth/insurance", "Insurance quote");
    await w.say("Insurance: a simulated quote for term life or health cover", 300);
    await page.getByLabel("Plan", { exact: true }).selectOption({ index: 0 });
    const cover = await hintAmount(page, "Cover");
    await page.getByLabel("Cover", { exact: true }).fill(cover.toFixed(2));
    await page.getByLabel("Your age band", { exact: true }).selectOption({ index: 2 });
    await page.getByRole("checkbox", { name: /age band being used/ }).check();
    await w.hold(800);
    const { body } = await w.api<{ outcome?: string }>(
      "POST",
      "/api/v1/wealth/insurance/quote",
      () => page.getByRole("button", { name: "Get a quote" }).click(),
    );
    await expect(page.getByRole("region", { name: "Quote" })).toBeVisible();
    await w.say(
      `The quote: premium and outcome (${String(body.outcome ?? "?").toLowerCase()})`,
      2800,
    );
    if (body.outcome === "DECLINED") return ["DIFF", "quote DECLINED; no application"];
    await w.api("POST", "/api/v1/wealth/insurance/applications", () =>
      page.getByRole("button", { name: "Apply", exact: true }).click(),
    );
    await expect(page.getByRole("heading", { name: "Application recorded" })).toBeVisible();
    await w.say("Application recorded (simulated: no policy is issued)", 2500);
    return ["PASS", `quote ${body.outcome ?? "?"}; applied`];
  });
}

// ===================================================================== 3. staff: fraud analyst

interface LabRun {
  transfers: TransferOut[];
}

async function runLab(
  w: Walkthrough,
  page: Page,
  code: string,
  title: string,
  signIns: number,
): Promise<LabRun> {
  await w.signInBudget(signIns);
  const pending: Promise<TransferOut>[] = [];
  const onResponse = (r: import("@playwright/test").Response) => {
    if (r.request().method() === "POST" && pathOf(r) === "/api/v1/transfers") {
      pending.push(r.json() as Promise<TransferOut>);
    }
  };
  page.on("response", onResponse);
  const card = page.getByRole("button", { name: `Run ${code} ${title}` });
  await w.dead(() => card.scrollIntoViewIfNeeded());
  await card.click();
  const heading = page.getByRole("heading", {
    name: new RegExp(`^${code} ${title}: `),
  });
  await w.fast(() => expect(heading).toHaveText(/: (finished|stopped)$/, { timeout: 90_000 }));
  page.off("response", onResponse);
  await expect(heading).toHaveText(/finished$/);
  await w.dead(() => heading.scrollIntoViewIfNeeded());
  return { transfers: await Promise.all(pending) };
}

async function openAlertFor(w: Walkthrough, page: Page, t: TransferOut): Promise<AlertDetail> {
  const [summary] = await w.apiGet<AlertSummary[]>(
    `/api/v1/fraud/alerts?status=OPEN&transaction_id=${t.id}`,
  );
  if (!summary) throw new Error(`no OPEN alert for transfer ${t.id}`);
  await w.go(`/staff/fraud/${summary.id}`, /^Risk \d+ of 100/);
  return w.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${summary.id}`);
}

async function decide(
  w: Walkthrough,
  page: Page,
  alert: AlertDetail,
  action: "Release" | "Reject",
  note: string,
): Promise<string> {
  await page.getByLabel("Note (optional, kept in the audit trail)").fill(note);
  await page.getByRole("button", { name: action, exact: true }).click();
  const confirm = page.getByRole("dialog");
  await expect(confirm).toBeVisible();
  await w.hold(900);
  await w.api("POST", /\/decision$/, () =>
    confirm.getByRole("button", { name: action, exact: true }).click(),
  );
  await expect(page.getByText("Decision recorded.")).toBeVisible();
  const after = await w.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${alert.id}`);
  return after.transaction_status;
}

async function staffAnalyst(
  w: Walkthrough,
  page: Page,
  aliceHeld: TransferOut | null,
): Promise<void> {
  w.chapter("Fraud analyst", true);
  await w.step("fraud analyst signs in with MFA", { highlight: true }, async () => {
    await w.say("Staff sign in with a password and an authenticator code: the fraud analyst", 300);
    await w.signInWithCode(ANALYST, "/staff/monitoring", "Security monitoring");
    return ["PASS", "password + TOTP accepted"];
  });

  await w.step("security monitoring", {}, async () => {
    await w.say(
      "Security monitoring: open investigations, login anomalies, high-risk customers",
      3000,
    );
    await w.reveal(page.getByRole("heading", { name: "Login anomalies" }));
    await w.hold(2200);
    await w.reveal(page.getByRole("heading", { name: "Recent fraud events" }));
    await w.hold(1800);
    return ["PASS", "monitoring shown"];
  });

  const lab: Record<string, LabRun> = {};
  await w.step("fraud demo lab: F1 APP scam", { highlight: true }, async () => {
    await w.go("/staff/fraud-lab", "Fraud demo lab");
    await w.say(
      "The fraud demo lab replays known fraud patterns through the real customer API",
      300,
    );
    await page.getByLabel("Customer password").fill(w.password);
    await w.say("F1, APP scam: a brand-new payee, then INR 30,000.00", 300);
    lab.F1 = await runLab(w, page, "F1", "APP scam", 1);
    const t = lab.F1.transfers[0];
    if (t?.status === "DECLINED") {
      // demo-script.md section 7 and the model report (section 10): the APP scam can score BLOCK.
      await w.say("Blocked outright: the top risk band. The lab shows the score and reasons", 3500);
      return ["DIFF", "F1 status=DECLINED (BLOCK, documented)"];
    }
    if (!t || t.status !== "PENDING_REVIEW")
      throw new Error(`F1 gave ${t?.status ?? "no transfer"}`);
    await w.say("Held for review. The lab shows the score, decision and reasons", 3500);
    return ["PASS", `F1 status=${t.status}`];
  });

  let forward: TransferOut | null = null;
  await w.step("fraud demo lab: F6 mule pass-through", { highlight: true }, async () => {
    await w.say("F6, mule pass-through: Bob pays Carol, and Carol forwards 95% straight out", 300);
    let run = await runLab(w, page, "F6", "Mule pass-through", 2);
    const inbound = run.transfers[0];
    if (inbound?.status === "PENDING_REVIEW") {
      // demo-script.md 5.4: on a fresh seed the inbound is held; release it, then run F6 again.
      await w.say("Bob's payment is held too: the analyst releases it and the lab runs again", 300);
      const alert = await openAlertFor(w, page, inbound);
      const status = await decide(w, page, alert, "Release", "verified with the sender");
      if (status !== "POSTED") throw new Error(`inbound after RELEASE is ${status}`);
      await w.go("/staff/fraud-lab", "Fraud demo lab");
      await page.getByLabel("Customer password").fill(w.password);
      run = await runLab(w, page, "F6", "Mule pass-through", 2);
    }
    forward = run.transfers.at(-1) ?? null;
    if (!forward || forward.status !== "PENDING_REVIEW") {
      throw new Error(`F6 forward is ${forward?.status ?? "missing"}`);
    }
    await w.say("The forward is held for review", 3000);
    return ["PASS", `F6 forward ${forward.status}`];
  });

  for (const [code, title, signIns, caption] of [
    ["F2", "Velocity burst", 0, "F2, velocity burst: four transfers within seconds"],
    ["F3", "Account takeover", 4, "F3, account takeover: wrong passwords, then a new device"],
    ["F5", "Amount anomaly", 0, "F5, amount anomaly: far above her usual transfers"],
  ] as const) {
    await w.step(`fraud demo lab: ${code} ${title}`, { highlight: code === "F3" }, async () => {
      await w.say(caption, 300);
      lab[code] = await runLab(w, page, code, title, signIns);
      const statuses = lab[code]?.transfers.map((t) => t.status) ?? [];
      await w.hold(3000);
      return ["PASS", `${code} statuses=${statuses.join(",")}`];
    });
  }

  await w.step("fraud demo lab: F4 impossible travel (not runnable here)", {}, async () => {
    const card = page.getByRole("heading", { name: /F4/ });
    await w.reveal(card);
    await w.say("F4, impossible travel, needs a GeoIP database: the lab says so honestly", 2500);
    return ["PASS", "F4 explained, not run"];
  });

  await w.step("fraud alert queue", {}, async () => {
    await w.go("/staff/fraud", "Fraud alerts");
    await w.say("The alert queue: newest first, with the main reason and the risk", 3500);
    return ["PASS", "queue shown"];
  });

  if (aliceHeld !== null) {
    const t: TransferOut = aliceHeld;
    await w.step(
      "alert detail: rule points and SHAP reasons, then reject",
      { highlight: true },
      async () => {
        await w.say("Alice's flagged transfer: why was it flagged?", 300);
        const alert = await openAlertFor(w, page, t);
        await w.reveal(page.getByRole("heading", { name: "Why it was flagged" }));
        const sources = new Set(alert.reasons.map((r) => r.source ?? "?"));
        await w.say(
          `Risk ${alert.risk_score}: bank-rule points, model SHAP values and anomaly scores`,
          4500,
        );
        await w.reveal(page.getByRole("heading", { name: "Transaction" }));
        await w.say("Scored by: the exact model version, so the decision can be reproduced", 2500);
        await w.reveal(page.getByRole("heading", { name: "Decision" }));
        await w.say("The customer did not recognise the payee: reject as fraud", 300);
        const status = await decide(
          w,
          page,
          alert,
          "Reject",
          "customer did not recognise the payee",
        );
        if (status !== "DECLINED") throw new Error(`after REJECT the transfer is ${status}`);
        await w.say(
          t.status === "PENDING_REVIEW"
            ? "Rejected: the held money returns to Alice"
            : "Labelled as fraud: a blocked payment never moved any money",
          2200,
        );
        const outcome: Outcome = sources.has("MODEL") ? "PASS" : "DIFF";
        return [
          outcome,
          `score=${alert.risk_score} reasons=${codes(alert)} sources=${[...sources].join(",")} -> ${status}`,
        ];
      },
    );
  } else {
    throw new Error("Alice's transfer was neither held nor blocked: no alert to explain");
  }

  // Release the first lab payment that is on hold (F1 when it is held, else an F2 transfer).
  const releasable = [...(lab.F1?.transfers ?? []), ...(lab.F2?.transfers ?? [])].find(
    (t) => t.status === "PENDING_REVIEW",
  );
  await w.step("release a held payment", {}, async () => {
    if (!releasable) throw new Error("no lab payment on hold to release");
    await w.say("The analyst calls Carol, who confirms the payment: release it", 300);
    const alert = await openAlertFor(w, page, releasable);
    await w.reveal(page.getByRole("heading", { name: "Why it was flagged" }));
    await w.hold(1800);
    const status = await decide(w, page, alert, "Release", "customer confirmed by phone");
    if (status !== "POSTED") throw new Error(`after RELEASE the transfer is ${status}`);
    await w.say("Released: the held money is captured and the payment posted", 2200);
    return ["PASS", `score=${alert.risk_score} reasons=${codes(alert)} -> ${status}`];
  });

  await w.step("reject the mule forward (F6)", {}, async () => {
    const t = forward;
    if (t === null) throw new Error("no F6 forward");
    const alert = await openAlertFor(w, page, t);
    const mule = codes(alert).includes(MULE_FORWARD_CODE);
    await w.reveal(page.getByRole("heading", { name: "Why it was flagged" }));
    await w.say(
      mule
        ? "Mule pass-through: money in from another customer, straight out again"
        : "The forward is held on other signals: reject it",
      2500,
    );
    const status = await decide(w, page, alert, "Reject", "mule pattern");
    if (status !== "DECLINED") throw new Error(`after REJECT the forward is ${status}`);
    return [mule ? "PASS" : "DIFF", `reasons=${codes(alert)} -> ${status}`];
  });

  await w.step("resolve the other lab alerts", {}, async () => {
    const decided: string[] = [];
    for (const code of ["F1", "F2", "F3", "F5"]) {
      for (const t of lab[code]?.transfers ?? []) {
        if (t === releasable || (t.status !== "PENDING_REVIEW" && t.status !== "DECLINED"))
          continue;
        const [summary] = await w.apiGet<AlertSummary[]>(
          `/api/v1/fraud/alerts?status=OPEN&transaction_id=${t.id}`,
        );
        if (!summary) continue;
        await w.fast(async () => {
          await w.go(`/staff/fraud/${summary.id}`, /^Risk \d+ of 100/);
          const alert = await w.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${summary.id}`);
          await decide(w, page, alert, "Reject", `lab ${code}: confirmed fraud`);
        });
        decided.push(`${code}:${t.status}`);
      }
    }
    await w.say("Blocked payments are labelled too, for reporting", 1200);
    return ["PASS", `rejected ${decided.join(",") || "none open"}`];
  });

  await w.step("fraud dashboard: statistics charts", { highlight: true }, async () => {
    await w.api("GET", "/api/v1/fraud/stats", () =>
      w.go("/staff/fraud-dashboard", "Fraud dashboard"),
    );
    await w.say("The fraud dashboard: payments scored, hold and block rates, scoring time", 3000);
    await w.reveal(page.getByRole("heading", { name: "Decisions over time" }));
    await w.say("Decisions per day, and the hold and block rates", 2800);
    await w.reveal(page.getByRole("heading", { name: "Risk score distribution" }));
    await w.hold(2200);
    await w.reveal(page.getByRole("heading", { name: "Top reasons" }));
    await w.say("The reasons behind them", 2500);
    return ["PASS", "dashboard shown"];
  });
  await w.signOut();
}

// ================================================================= 4. staff: compliance officer

async function staffCompliance(w: Walkthrough, page: Page): Promise<void> {
  w.chapter("Compliance officer", true);
  await w.step("compliance officer signs in with MFA", {}, async () => {
    await w.say("The compliance officer signs in (password and code)", 300);
    await w.signInWithCode(COMPLIANCE, "/staff/compliance", "Compliance & risk");
    return ["PASS", "password + TOTP accepted"];
  });

  await w.step("compliance dashboard and exceptions", { highlight: true }, async () => {
    await w.say(
      "Compliance & risk: regulatory monitoring tiles and the exceptions to decide",
      3500,
    );
    const exceptions = page.getByRole("heading", { name: "Exceptions" });
    await w.reveal(exceptions);
    const ack = page.getByRole("button", { name: "Acknowledge" });
    if ((await ack.count()) === 0) {
      await w.hold(2000);
      return ["DIFF", "no open exception to acknowledge"];
    }
    await ack.first().click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Reason").fill("Reviewed with the fraud team; tracking to closure");
    await w.hold(800);
    await w.api("POST", /^\/api\/v1\/compliance\/exceptions\/.+\/acknowledge$/, () =>
      dialog.getByRole("button", { name: "Acknowledge" }).click(),
    );
    await w.say("Acknowledged with a reason: stored in the hash-chained audit log", 2500);
    const kind = page.getByLabel("Kind");
    const kinds = await kind.locator("option").allTextContents();
    if (
      kinds.some((k) => /Undelivered notification/.test(k)) &&
      kinds.some((k) => /Scheduled transfer failure/.test(k))
    ) {
      await w.dead(() => page.evaluate(() => window.scrollTo(0, 0)));
      await kind.selectOption({ label: "Scheduled transfer failure" });
      await w.say("New kinds: failed scheduled transfers and undelivered notifications", 2500);
      await kind.selectOption({ label: "Undelivered notification" });
      await w.hold(2000);
      await kind.selectOption({ index: 0 });
    } else {
      w.pending(
        "compliance system exceptions",
        "no notification/scheduler exception kinds on this build",
      );
    }
    return ["PASS", "exception acknowledged"];
  });

  await w.step("audit log with hash-chain verification", { highlight: true }, async () => {
    await w.go("/staff/audit-log", "Compliance audit log");
    await w.say("The audit log: every security-relevant action, hash-chained", 2200);
    const { body } = await w.api<{ valid: boolean; checked: number }>(
      "GET",
      /\/audit-logs\/verify$/,
      () => page.getByRole("button", { name: "Verify hash chain" }).click(),
    );
    await expect(page.getByText("Hash chain intact.")).toBeVisible();
    if (!body.valid) throw new Error("the hash chain is not valid");
    await w.say(`Hash chain intact: ${body.checked} entries verified, no tampering`, 3500);
    return ["PASS", `valid=${body.valid} checked=${body.checked}`];
  });
  await w.signOut();
}

// ============================================================ 5. staff: relationship manager

async function staffRelationshipManager(w: Walkthrough, page: Page): Promise<void> {
  w.chapter("Relationship manager");
  await w.step("relationship manager signs in with MFA", {}, async () => {
    await w.say("The relationship manager signs in (password and code)", 300);
    await w.signInWithCode(RM, "/staff/insights", "Customer insights");
    return ["PASS", "password + TOTP accepted"];
  });

  await w.step("RM insights with the outreach log", {}, async () => {
    await w.say("Customer insights: churn risk and the best product match, per customer", 3000);
    const open = page.getByRole("button", { name: /^Open / }).first();
    await w.api("GET", /^\/api\/v1\/insights\/customers\/[^/]+$/, () => open.click());
    const flyout = page.getByRole("dialog");
    await expect(flyout.getByRole("heading", { name: "Churn risk" })).toBeVisible();
    await w.say("Churn risk, financial health and eligible products, with reasons", 3000);
    const profile = flyout.getByRole("heading", {
      name: "Behavioural profile",
    });
    if (await w.dead(() => present(profile))) {
      await w.reveal(profile);
      await w.say("The behavioural profile: product interests from app usage (with consent)", 3000);
    } else {
      w.pending(
        "behaviour profile in the customer flyout",
        "no 'Behavioural profile' section on this build",
      );
    }
    await w.reveal(flyout.getByRole("heading", { name: "Calls and notes" }));
    await flyout.getByLabel("Type").selectOption({ index: 0 });
    await flyout.getByLabel("Outcome").selectOption({ index: 0 });
    await flyout
      .locator("textarea")
      .last()
      .fill("Called about the fixed deposit offer; follow up next week");
    await w.api("POST", /\/interactions$/, () =>
      flyout.getByRole("button", { name: "Log it" }).click(),
    );
    await w.say("Outreach logged on the customer record (and audited)", 2500);
    await flyout.getByRole("button", { name: "Close" }).first().click();
    return ["PASS", "interaction logged"];
  });

  await w.step("behavioural analytics", {}, async () => {
    await w.api("GET", /^\/api\/v1\/insights\/behaviour/, () =>
      w.go("/staff/behaviour", "Behavioural analytics"),
    );
    await w.say(
      "Behavioural analytics: segments, spending patterns, product affinity, engagement",
      3000,
    );
    await w.reveal(page.getByRole("heading", { name: "Product affinity" }));
    await w.hold(1800);
    await w.reveal(page.getByRole("heading", { name: "Engagement" }));
    await w.hold(2000);
    return ["PASS", "analytics shown"];
  });
  await w.signOut();
}

// ======================================================================= 6. staff: administrator

async function staffAdmin(w: Walkthrough, page: Page, newcomer: Newcomer | null): Promise<void> {
  w.chapter("Administrator");
  await w.step("administrator signs in with MFA", {}, async () => {
    await w.say("The administrator signs in (password and code)", 300);
    await w.signInWithCode(ADMIN, "/staff/admin", "Administration");
    return ["PASS", "password + TOTP accepted"];
  });

  await w.step("administration: create a staff user", {}, async () => {
    const create = page.getByRole("region", { name: "Create a staff user" });
    await w.say("Administration: create a staff user with a role", 300);
    const email = `walkthrough-staff-${tag()}${tag()}@example.com`;
    await w.fast(async () => {
      await create.getByLabel("Work e-mail", { exact: true }).fill(email);
      await create.getByLabel("Full name", { exact: true }).fill(`Ravi Analyst ${tag()}`);
      await create.getByLabel("Initial password", { exact: true }).fill(newPassword());
      await create.getByRole("checkbox", { name: "Fraud analyst", exact: true }).check();
    });
    const { status } = await w.api("POST", "/api/v1/admin/users", () =>
      create.getByRole("button", { name: "Create user", exact: true }).click(),
    );
    await expect(create.getByRole("status")).toContainText(`Created ${email} as Fraud analyst.`);
    await w.say("Created: they must set up an authenticator before using staff tools", 2500);
    return ["PASS", `create ${status}`];
  });

  await w.step("administration: sign-in lockouts", {}, async () => {
    const lockout = page.getByRole("region", { name: "Sign-in lockout" });
    await w.reveal(lockout);
    await w.say("Lockouts: inspect a user and clear a lockout (audited)", 300);
    // The new customer from chapter 1 (one wrong code on record), else the admin's own user.
    const userId = newcomer?.userId ?? (await w.apiGet<{ id: string }>("/api/v1/auth/me")).id;
    await lockout.getByLabel("User id", { exact: true }).fill(userId);
    await w.api("GET", /\/lockout$/, () =>
      lockout.getByRole("button", { name: "Inspect", exact: true }).click(),
    );
    await expect(lockout.getByText(/^(Locked|Not locked)$/).first()).toBeVisible();
    await w.hold(3000);
    return ["PASS", "inspected the new customer"];
  });

  await w.step("administration: application settings", {}, async () => {
    const panel = page.getByRole("region", { name: "Application settings" });
    await w.reveal(panel);
    await w.say(
      "Business limits that change without a deployment, each change with a reason",
      3500,
    );
    return ["PASS", "settings shown"];
  });
  await w.signOut();
}

// ====================================================================== 7. model accuracy page

async function modelAccuracy(w: Walkthrough, page: Page, outDir: string): Promise<void> {
  w.chapter("Fraud model accuracy", true);
  const report = join(outDir, "model-report", "index.html");
  if (!existsSync(report)) {
    throw new Error(`run render-model-report.py first: ${report} is missing`);
  }
  await w.step("model accuracy results", { highlight: true }, async () => {
    await w.dead(() => page.goto(pathToFileURL(report).href));
    await w.say("How good is the fraud model? From the evaluation report (synthetic data)", 3000);
    for (const [id, caption, ms] of [
      [
        "four-bars",
        "The four promotion bars: hybrid recall at REVIEW against the rules alone",
        5000,
      ],
      ["components", "Each component, and the rules-only fail-safe", 3500],
      ["pr-curves", "Precision-recall curves: hybrid, LightGBM, Isolation Forest, rules", 4500],
      ["cost-curve", "Choosing the threshold by the cost of errors", 3500],
      ["shap", "Which features drive the model (SHAP)", 3500],
    ] as const) {
      await w.reveal(page.locator(`#${id}`));
      await w.say(caption, ms);
    }
    return ["PASS", "report page shown"];
  });
}

// =========================================================================== 8. Grafana

/** Grafana shows "Cancel" in the refresh picker while any panel is querying; panels load as they
 * come into view, so wait until it has stayed away for two seconds (at most 90 seconds). */
async function grafanaSettled(page: Page): Promise<void> {
  const busy = page.getByText("Cancel", { exact: true });
  let quiet = 0;
  for (let waited = 0; waited < 90_000 && quiet < 2_000; waited += 250) {
    quiet = (await busy.count()) > 0 && (await busy.first().isVisible()) ? 0 : quiet + 250;
    await page.waitForTimeout(250);
  }
}

async function grafana(w: Walkthrough, page: Page): Promise<void> {
  w.chapter("Operations dashboards (Grafana)", true);
  await w.step("grafana sign-in", {}, async () => {
    await w.dead(async () => {
      await page.goto(`${GRAFANA}/login`);
      await page.locator('input[name="user"]').fill("admin");
      await page.locator('input[name="password"]').fill(grafanaPassword());
      await page.getByRole("button", { name: /log in/i }).click();
      const skip = page.getByRole("button", { name: /skip/i });
      if (await present(skip)) await skip.click();
      await page.waitForURL((url) => !url.pathname.startsWith("/login"));
    });
    const health = await page.request.get(`${GRAFANA}/api/datasources/uid/prometheus/health`);
    if (health.status() !== 200) throw new Error(`Prometheus datasource health ${health.status()}`);
    return ["PASS", "signed in; Prometheus datasource healthy"];
  });

  for (const [uid, name, caption] of [
    [
      "pbip-api",
      "API latency and errors",
      "API: request rate, errors and p50/p95/p99 latency per route",
    ],
    [
      "pbip-fraud",
      "Fraud decisions",
      "Fraud decisions, scoring latency, and model input drift (PSI)",
    ],
    ["pbip-availability", "Availability", "Availability: health probes against the 99% target"],
  ] as const) {
    await w.step(`grafana: ${name}`, { highlight: uid === "pbip-fraud" }, async () => {
      const dash = await page.request.get(`${GRAFANA}/api/dashboards/uid/${uid}`);
      if (dash.status() !== 200) throw new Error(`dashboard ${uid}: ${dash.status()}`);
      await w.dead(async () => {
        // refresh=1h: no auto-refresh while it is on screen. While panels query, the refresh
        // picker shows "Cancel"; the dashboard is drawn once that is gone.
        await page.goto(`${GRAFANA}/d/${uid}?orgId=1&from=now-1h&to=now&refresh=1h&kiosk`);
        await page.waitForLoadState("load");
        await page.waitForTimeout(1500);
        await grafanaSettled(page);
        await page.waitForTimeout(800);
      });
      await w.say(caption, 5000);
      if (uid === "pbip-fraud") {
        await w.dead(() => page.mouse.move(960, 600));
        await page.mouse.wheel(0, 900);
        await w.dead(() => grafanaSettled(page));
        await w.say("Drift: the Population Stability Index of the model's inputs", 4000);
      }
      return ["PASS", `dashboard ${uid}`];
    });
  }
}
