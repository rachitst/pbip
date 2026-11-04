import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import type { Page, Response } from "@playwright/test";
import {
  enterSignInCode,
  inr,
  reviewTransfer,
  signIn,
  staffCode,
} from "../../apps/web/e2e/support";

// TASK-093: records the fallback demo video. It drives the WEB UI through the storyline of
// demo/demo-script.md, in the order and with the amounts of the lead's API-level live run:
//   1. Alice INR 35,000.00 to a new payee is held for review; the analyst rejects it.
//   2. Alice INR 20,000.00 to Bob Demo (INR 40,000.00 once Bob Demo is over 24 h old) asks for
//      her code, then posts.
//   3. Carol (fraud demo lab F1) INR 30,000.00 to a new payee is held; the analyst releases it.
//   4. F6: Bob pays Carol INR 24,000.00 (on a fresh seed held, so the analyst releases it first);
//      Carol forwards INR 22,800.00, held with MULE_PASS_THROUGH; the analyst rejects it.
//   5. Six assistant questions as Alice.  6. The compliance officer verifies the audit hash chain.
// Every money step checks the API's answer (read from the browser's own responses) and stops the
// run loudly when the outcome is not one the demo script allows. Outcomes the script documents as
// alternatives (section 5 and 7) are reported as DIFF and the run continues.
// One browser page records everything, so users take turns by signing out and in. Passwords,
// codes and tokens are never printed.

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../..");
const VIDEO_SIZE = { width: 1920, height: 1080 };
const PACE = Number(process.env.DEMO_PACE ?? "1");
const CAPTIONS = process.env.DEMO_CAPTIONS !== "0";
/** Sign-in and MFA verify share 10 requests per minute per IP; stay one below it. */
const SIGN_IN_BUDGET = 9;

const ALICE = "alice@demo.pbip";
const BOB_NAME = "Bob Demo";
const CAROL = "carol@demo.pbip";
const ANALYST = "analyst@demo.pbip";
const COMPLIANCE = "compliance@demo.pbip";

const REVIEW_AMOUNT = Number(process.env.DEMO_REVIEW_AMOUNT_MINOR ?? "3500000"); // INR 35,000.00
const F1_AMOUNT = 3_000_000; // INR 30,000.00, fixed in fraudLab.ts
const MULE_INBOUND = 2_400_000; // INR 24,000.00, fixed in fraudLab.ts
const MULE_FORWARD = 2_280_000; // 95%: INR 22,800.00
const DAY_MS = 24 * 60 * 60 * 1000;

const QUESTIONS: { text: string; want: "answer" | "refuse" | "llm" }[] = [
  { text: "how much did I spend last month?", want: "answer" },
  { text: "can I afford a 5 lakh car loan over 5 years?", want: "answer" },
  { text: "what will my balance be at the end of the month?", want: "answer" },
  { text: "show my recent transactions", want: "answer" },
  { text: "show me bob's account balance", want: "refuse" },
  { text: "am I doing ok money-wise?", want: "llm" },
];

type Outcome = "PASS" | "DIFF" | "FAIL";
interface StepResult {
  step: string;
  outcome: Outcome;
  detail: string;
}
interface TransferOut {
  id: string;
  status: string;
  amount_minor: number;
}
interface AlertSummary {
  id: string;
  amount_minor: number;
  customer_name?: string | null;
  status: string;
  created_at: string;
}
interface AlertDetail {
  id: string;
  transaction_id: string;
  transaction_status: string;
  risk_score: number;
  decision: string;
  reasons: { code?: string }[];
}
interface PayeeOut {
  name: string;
  created_at: string;
}

/** The documented local demo password: PBIP_DEMO_PASSWORD if set (what the seed was run with),
 * else the APP_ENV=local constant in scripts/seed_dev.py (demo-script.md section 1). Never logged. */
function demoPassword(): string {
  const fromEnv = process.env.PBIP_DEMO_PASSWORD;
  if (fromEnv) return fromEnv;
  const seed = readFileSync(join(REPO_ROOT, "scripts/seed_dev.py"), "utf8");
  const constant = /^DEMO_PASSWORD = "([^"]+)"/m.exec(seed)?.[1];
  if (!constant)
    throw new Error("set PBIP_DEMO_PASSWORD: no DEMO_PASSWORD constant in seed_dev.py");
  return constant;
}

function tag(): string {
  return randomUUID().slice(0, 4);
}

/** A 12-digit account number for a payee at another bank (names there are not checked). */
function externalAccountNumber(): string {
  return String(BigInt(`0x${randomUUID().replace(/-/g, "").slice(0, 12)}`) % 10n ** 12n).padStart(
    12,
    "0",
  );
}

function pathOf(response: Response): string {
  return new URL(response.url()).pathname;
}

class Recorder {
  readonly results: StepResult[] = [];
  private caption = "";
  private signIns: number[] = [];
  /** Sign-in and MFA requests made by the whole run (reported, for the rate-limit budget). */
  signInRequests = 0;
  /** The analyst's bearer token, as the app last sent it to the fraud API, for read-only checks of
   * what the page shows. Only fraud API calls count: the lab calls the customer API with the demo
   * customers' own tokens from the same page. */
  private token = "";

  constructor(
    readonly page: Page,
    readonly password: string,
  ) {
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (
        request.method() === "POST" &&
        (path === "/api/v1/auth/login" || path === "/api/v1/auth/mfa/verify")
      ) {
        this.signIns.push(Date.now());
        this.signInRequests += 1;
      }
      const auth = request.headers()["authorization"];
      if (path.startsWith("/api/v1/fraud/") && auth?.startsWith("Bearer ")) this.token = auth;
    });
  }

  record(step: string, outcome: Outcome, detail: string): void {
    this.results.push({ step, outcome, detail });
    // Results only: amounts, statuses, scores. Never a credential.
    console.log(`${outcome} ${step} :: ${detail}`);
  }

  async pause(ms: number): Promise<void> {
    await this.page.waitForTimeout(Math.round(ms * PACE));
  }

  /** A small overlay for the viewer. aria-hidden and pointer-events: none, so it never changes
   * what the app does or what a locator finds. */
  async say(text: string, hold = 2500): Promise<void> {
    this.caption = text;
    await this.showCaption();
    await this.pause(hold);
  }

  async showCaption(): Promise<void> {
    if (!CAPTIONS || this.caption === "") return;
    await this.page.evaluate((text) => {
      const id = "pbip-demo-caption";
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement("div");
        el.id = id;
        el.setAttribute("aria-hidden", "true");
        Object.assign(el.style, {
          // Centred over the app header, whose middle is empty: never over a form or a button.
          position: "fixed",
          top: "4px",
          left: "50%",
          transform: "translateX(-50%)",
          maxWidth: "54vw",
          textAlign: "center",
          zIndex: "2147483647",
          pointerEvents: "none",
          background: "rgba(15, 23, 42, 0.92)",
          color: "#ffffff",
          font: "600 19px/1.3 system-ui, -apple-system, 'Segoe UI', sans-serif",
          padding: "8px 18px",
          borderRadius: "8px",
          boxShadow: "0 6px 24px rgba(0, 0, 0, 0.35)",
        });
        document.body.appendChild(el);
      }
      el.textContent = text;
    }, this.caption);
  }

  /** Waits (rarely) so the next sign-in stays inside the per-IP rate limit. */
  async signInBudget(requests: number): Promise<void> {
    for (;;) {
      const now = Date.now();
      this.signIns = this.signIns.filter((t) => now - t < 61_000);
      if (this.signIns.length + requests <= SIGN_IN_BUDGET) return;
      const oldest = this.signIns[0] ?? now;
      await this.page.waitForTimeout(61_000 - (now - oldest) + 250);
    }
  }

  async apiGet<T>(path: string): Promise<T> {
    const response = await this.page.request.get(path, {
      headers: { Authorization: this.token },
    });
    expect(response.status(), `GET ${path}`).toBe(200);
    return (await response.json()) as T;
  }

  // ------------------------------------------------------------------------------ sign-in

  async signOut(): Promise<void> {
    await this.page.getByRole("button", { name: "Sign out" }).click();
    await expect(this.page.getByRole("button", { name: "Sign in" })).toBeVisible();
    this.token = "";
  }

  async signInWithCode(email: string, path: string): Promise<void> {
    await this.signInBudget(2);
    await signIn(this.page, email, this.password, path);
    await this.showCaption();
    await this.pause(800);
    await enterSignInCode(this.page, staffCode(email));
  }

  async signInCustomer(email: string, withCode: boolean): Promise<void> {
    if (withCode) {
      await this.signInWithCode(email, "/");
    } else {
      await this.signInBudget(1);
      await signIn(this.page, email, this.password);
      await this.showCaption();
    }
    await expect(this.page.getByRole("heading", { name: "My accounts" })).toBeVisible();
    await this.pause(1500);
  }

  async signInStaff(email: string, path: string, heading: string): Promise<void> {
    await this.signInWithCode(email, path);
    await expect(this.page.getByRole("heading", { name: heading })).toBeVisible();
    await this.showCaption();
    await this.pause(1200);
  }

  // ------------------------------------------------------------------------- customer steps

  /** Payees -> Add payee at another bank. Returns the payees list the page loaded. */
  async addExternalPayee(name: string): Promise<PayeeOut[]> {
    const listed = this.page.waitForResponse(
      (r) => r.request().method() === "GET" && pathOf(r) === "/api/v1/payees",
    );
    await this.page.getByRole("link", { name: "Payees" }).click();
    const payees = (await (await listed).json()) as PayeeOut[];
    await this.pause(1200);
    await this.page.getByRole("button", { name: "Add payee" }).click();
    await this.page.getByLabel("Payee name").fill(name);
    await this.page.getByLabel("Account number").fill(externalAccountNumber());
    await this.page.getByLabel("Bank code").fill("EXTB0001");
    await this.pause(800);
    await this.page.getByRole("button", { name: "Add and check name" }).click();
    await expect(this.page.getByText("This account is at another bank")).toBeVisible();
    await expect(this.page.getByRole("row").filter({ hasText: name })).toContainText("Verified");
    await this.pause(1500);
    return payees;
  }

  /** Accounts -> Send money -> Check and confirm -> Send. Returns the API's answer. */
  async sendMoney(payee: string, amount: number, reference: string): Promise<TransferOut> {
    await reviewTransfer(this.page, payee, amount, reference);
    await this.pause(1500);
    const sent = this.page.waitForResponse(
      (r) => r.request().method() === "POST" && pathOf(r) === "/api/v1/transfers",
    );
    await this.page.getByRole("button", { name: "Send money" }).click();
    const response = await sent;
    const body = (await response.json()) as TransferOut;
    expect(response.status(), "POST /transfers").toBeLessThan(300);
    return body;
  }

  // -------------------------------------------------------------------------- analyst steps

  /** Fraud alerts -> the open alert raised for exactly this transfer. The queue shows customer,
   * reason and amount; the alert id comes from the same API filtered by transaction id. */
  async openAlertFor(transfer: TransferOut): Promise<AlertDetail> {
    const [summary] = await this.apiGet<AlertSummary[]>(
      `/api/v1/fraud/alerts?status=OPEN&transaction_id=${transfer.id}`,
    );
    if (!summary) {
      throw new Error(`no OPEN alert for transfer ${transfer.id} (${inr(transfer.amount_minor)})`);
    }
    await this.page.getByRole("link", { name: "Fraud alerts" }).click();
    const link = this.page.locator(`a[href="/staff/fraud/${summary.id}"]`);
    await expect(link).toBeVisible();
    await link.scrollIntoViewIfNeeded();
    await this.pause(2500);
    await link.click();
    await expect(this.page.getByRole("heading", { name: /^Risk \d+ of 100/ })).toBeVisible();
    return this.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${summary.id}`);
  }

  /** On an open alert page: note, Release or Reject, confirm. Returns the transfer's new status. */
  async decide(alert: AlertDetail, action: "Release" | "Reject", note: string): Promise<string> {
    await this.page.getByRole("heading", { name: "Why it was flagged" }).scrollIntoViewIfNeeded();
    await this.pause(3000);
    await this.page.getByLabel("Note (optional, kept in the audit trail)").fill(note);
    await this.pause(800);
    await this.page.getByRole("button", { name: action, exact: true }).click();
    const confirm = this.page.getByRole("dialog");
    await expect(confirm).toBeVisible();
    await this.pause(1500);
    await confirm.getByRole("button", { name: action, exact: true }).click();
    await expect(this.page.getByText("Decision recorded.")).toBeVisible();
    await this.pause(2500);
    const after = await this.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${alert.id}`);
    return after.transaction_status;
  }

  async openLab(): Promise<void> {
    await this.page.getByRole("link", { name: "Fraud demo lab" }).click();
    await expect(this.page.getByRole("heading", { name: "Demo customers" })).toBeVisible();
    await expect(this.page.getByLabel("Customer email")).toHaveValue(CAROL);
    await this.page.getByLabel("Customer password").fill(this.password);
    await this.pause(1000);
  }

  /** Runs a lab scenario and returns the transfers it created, in order, as the API answered. */
  async runLab(code: "F1" | "F6", title: string, signInRequests: number): Promise<TransferOut[]> {
    await this.signInBudget(signInRequests);
    const pending: Promise<TransferOut>[] = [];
    const onResponse = (r: Response) => {
      if (r.request().method() === "POST" && pathOf(r) === "/api/v1/transfers") {
        pending.push(r.json() as Promise<TransferOut>);
      }
    };
    this.page.on("response", onResponse);
    const card = this.page.getByRole("button", {
      name: `Run ${code} ${title}`,
    });
    await card.scrollIntoViewIfNeeded();
    await this.pause(1500);
    await card.click();
    const heading = this.page.getByRole("heading", {
      name: new RegExp(`^${code} ${title}: `),
    });
    await expect(heading).toHaveText(/: (finished|stopped)$/, {
      timeout: 60_000,
    });
    this.page.off("response", onResponse);
    await heading.scrollIntoViewIfNeeded();
    await expect(heading).toHaveText(/finished$/);
    await this.pause(3500);
    return Promise.all(pending);
  }

  async openLabAlert(index: number): Promise<AlertDetail> {
    const link = this.page.locator("article.lab-outcome").nth(index).getByRole("link", {
      name: "Open the alert",
    });
    const href = (await link.getAttribute("href")) ?? "";
    const id = href.split("/").pop() ?? "";
    await link.click();
    await expect(this.page.getByRole("heading", { name: /^Risk \d+ of 100/ })).toBeVisible();
    return this.apiGet<AlertDetail>(`/api/v1/fraud/alerts/${id}`);
  }
}

function codes(alert: AlertDetail): string[] {
  return alert.reasons.map((r) => r.code ?? "?");
}

test("PBIP fallback demo recording (TASK-093)", async ({ browser }) => {
  const outDir = resolve(process.env.DEMO_VIDEO_DIR ?? join(HERE, "out"));
  mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const context = await browser.newContext({
    viewport: VIDEO_SIZE,
    recordVideo: { dir: join(outDir, `.raw-${stamp}`), size: VIDEO_SIZE },
  });
  const page = await context.newPage();
  const r = new Recorder(page, demoPassword());
  let failed: unknown = null;

  const step = async (name: string, body: () => Promise<[Outcome, string]>) => {
    try {
      const [outcome, detail] = await test.step(name, body);
      r.record(name, outcome, detail);
    } catch (error) {
      r.record(name, "FAIL", error instanceof Error ? (error.message.split("\n")[0] ?? "") : "");
      throw error;
    }
  };

  try {
    // ------------------------------------------------------------------- 1. Alice, review
    await page.goto("/");
    await r.say("PBIP, a simulated retail bank with an explainable fraud engine", 3000);
    await r.say("Alice signs in with her password, then her authenticator code", 1000);
    await step("alice sign-in with authenticator", async () => {
      await r.signInCustomer(ALICE, true);
      return ["PASS", "password + TOTP accepted"];
    });

    // Filled in by the steps below (closures), read by the later ones.
    const held: {
      alicePayees: PayeeOut[];
      aliceReview: TransferOut | null;
      aliceStepUp: TransferOut | null;
      forward: TransferOut | null;
    } = {
      alicePayees: [],
      aliceReview: null,
      aliceStepUp: null,
      forward: null,
    };
    await step("alice 35,000 to a new payee -> held for review", async () => {
      await r.say(`Alice adds a new payee at another bank, then sends it ${inr(REVIEW_AMOUNT)}`);
      const payee = `Asha Traders ${tag()}`;
      held.alicePayees = await r.addExternalPayee(payee);
      const t = await r.sendMoney(payee, REVIEW_AMOUNT, `Demo invoice ${tag()}`);
      if (t.status === "PENDING_REVIEW") {
        held.aliceReview = t;
        await expect(page.getByRole("status").filter({ hasText: "being reviewed" })).toBeVisible();
        await r.say("Held for review. The customer is never told the score or the reasons", 4000);
        return ["PASS", `status=${t.status}`];
      }
      if (t.status === "DECLINED") {
        // demo-script.md section 7: the model can outscore the rules into BLOCK.
        await r.say("Declined: the top risk band. The money did not move", 4000);
        return ["DIFF", "status=DECLINED (BLOCK, documented in section 7); nothing to reject"];
      }
      throw new Error(`expected PENDING_REVIEW, got ${t.status}`);
    });

    // ----------------------------------------------------------------- 2. Alice, step-up
    await step("alice to Bob Demo -> step-up -> posted", async () => {
      const bob = held.alicePayees.find((p) => p.name === BOB_NAME);
      if (!bob) throw new Error("Alice has no payee named Bob Demo");
      const ageMs = Date.now() - Date.parse(bob.created_at);
      // demo-script.md 5.2: INR 20,000.00 while Bob Demo is under 24 h old, else INR 40,000.00.
      const amount = Number(
        process.env.DEMO_STEPUP_AMOUNT_MINOR ?? (ageMs < DAY_MS ? "2000000" : "4000000"),
      );
      await r.say(`Alice sends ${inr(amount)} to her payee Bob Demo: unusually large for her`);
      const t = await r.sendMoney(BOB_NAME, amount, `Demo rent share ${tag()}`);
      const note = `amount=${inr(amount)} (Bob Demo ${Math.round(ageMs / 3_600_000)} h old)`;
      if (t.status === "PENDING_REVIEW") {
        // 5.2: if the model raises it to 60 or more it is held; the analyst releases it.
        held.aliceStepUp = t;
        await r.say("Held for review instead of a step-up: the analyst will release it", 3500);
        return ["DIFF", `${note} status=PENDING_REVIEW (documented in 5.2)`];
      }
      if (t.status !== "PENDING_STEP_UP") {
        throw new Error(`${note}: expected PENDING_STEP_UP, got ${t.status}`);
      }
      const dialog = page.getByRole("dialog", { name: "Confirm it's you" });
      await expect(dialog).toContainText(inr(amount));
      await r.say("Step-up: the bank asks for Alice's authenticator code before any money moves");
      await dialog.getByLabel("Authenticator code").fill(staffCode(ALICE));
      await r.pause(800);
      const confirmed = page.waitForResponse(
        (res) => res.request().method() === "POST" && pathOf(res).endsWith("/step-up"),
      );
      await dialog.getByRole("button", { name: "Confirm transfer" }).click();
      const after = (await (await confirmed).json()) as TransferOut;
      if (after.status !== "POSTED") throw new Error(`${note}: step-up gave ${after.status}`);
      await expect(page.getByRole("status").filter({ hasText: "Sent" })).toContainText(inr(amount));
      await r.say("Code accepted: the transfer is posted to the ledger", 3500);
      return ["PASS", `${note} PENDING_STEP_UP -> step-up -> ${after.status}`];
    });

    // ------------------------------------------------ analyst: Alice's held 35,000 (reject)
    await r.say("A fraud analyst signs in (password and authenticator code)", 1000);
    await r.signOut();
    await step("analyst sign-in with authenticator", async () => {
      await r.signInStaff(ANALYST, "/staff/fraud", "Fraud alerts");
      return ["PASS", "password + TOTP accepted"];
    });
    const review = held.aliceReview;
    if (review !== null) {
      await step("analyst rejects alice's held 35,000", async () => {
        await r.say("Alice's held transfer is at the top of the queue. Why was it flagged?");
        const alert = await r.openAlertFor(review);
        await r.say(`Score ${alert.risk_score}: every point is explained (rules and model)`, 3000);
        const status = await r.decide(alert, "Reject", "customer did not recognise the payee");
        if (status !== "DECLINED") throw new Error(`after REJECT the transfer is ${status}`);
        return ["PASS", `score=${alert.risk_score} reasons=${codes(alert).join(",")} -> ${status}`];
      });
    }
    const stepUpHeld = held.aliceStepUp;
    if (stepUpHeld !== null) {
      await step("analyst releases alice's held Bob Demo transfer", async () => {
        const alert = await r.openAlertFor(stepUpHeld);
        const status = await r.decide(alert, "Release", "verified with the customer");
        if (status !== "POSTED") throw new Error(`after RELEASE the transfer is ${status}`);
        return ["DIFF", `score=${alert.risk_score} -> RELEASE -> ${status}`];
      });
    }

    // ---------------------------------------------------- 3. Carol F1 (lab): held, release
    await step("carol F1 30,000 to a new payee held -> release -> posted", async () => {
      await r.say("Fraud demo lab: F1, an APP scam. Carol pays a brand-new payee INR 30,000.00");
      await r.openLab();
      const [t] = await r.runLab("F1", "APP scam", 1);
      if (!t || t.amount_minor !== F1_AMOUNT) throw new Error("the lab made no F1 transfer");
      if (t.status !== "PENDING_REVIEW")
        throw new Error(`expected PENDING_REVIEW, got ${t.status}`);
      await r.say("Held for review. The lab shows the engine's score and reasons", 3000);
      const alert = await r.openLabAlert(0);
      await r.say("The analyst calls Carol, who confirms the payment: release it");
      const status = await r.decide(alert, "Release", "customer confirmed by phone");
      if (status !== "POSTED") throw new Error(`after RELEASE the transfer is ${status}`);
      return ["PASS", `score=${alert.risk_score} reasons=${codes(alert).join(",")} -> ${status}`];
    });

    // ------------------------------------------------------- 4. F6 mule pass-through (lab)
    await step("F6 bob pays carol 24,000 (inbound)", async () => {
      await r.say("F6, mule pass-through: Bob pays Carol INR 24,000.00, and Carol forwards it");
      await r.openLab();
      const made = await r.runLab("F6", "Mule pass-through", 2);
      const inbound = made[0];
      if (!inbound || inbound.amount_minor !== MULE_INBOUND) throw new Error("no F6 inbound");
      if (inbound.status === "POSTED") {
        const out = made[1];
        if (!out) throw new Error("the lab did not forward the inbound");
        held.forward = out;
        return ["PASS", "inbound POSTED; the lab forwarded 95%"];
      }
      if (inbound.status !== "PENDING_REVIEW") {
        throw new Error(`inbound: expected POSTED or PENDING_REVIEW, got ${inbound.status}`);
      }
      // demo-script.md 5.4: on a fresh seed Carol is a new payee for Bob, so the inbound is held
      // and must be released BEFORE Carol forwards (a held transfer has not arrived).
      await r.say("Bob's payment is held too: the analyst releases it before Carol forwards", 3000);
      const alert = await r.openLabAlert(0);
      const status = await r.decide(alert, "Release", "verified with the sender");
      if (status !== "POSTED") throw new Error(`after RELEASE the inbound is ${status}`);
      return [
        "DIFF",
        `inbound PENDING_REVIEW score=${alert.risk_score} reasons=${codes(alert).join(",")} -> RELEASE -> POSTED (documented in 5.4)`,
      ];
    });

    if (held.forward === null) {
      await step("F6 carol forwards 22,800 to a new external payee", async () => {
        await r.say("Carol signs in and forwards 95% to a new outside account", 1000);
        await r.signOut();
        await r.signInCustomer(CAROL, false);
        const payee = `Quick Cash Exports ${tag()}`;
        await r.addExternalPayee(payee);
        const t = await r.sendMoney(payee, MULE_FORWARD, `Demo forward ${tag()}`);
        if (t.status !== "PENDING_REVIEW") {
          throw new Error(`forward: expected PENDING_REVIEW, got ${t.status}`);
        }
        await expect(page.getByRole("status").filter({ hasText: "being reviewed" })).toBeVisible();
        await r.say("Held for review: money in from another customer, straight out again", 3500);
        held.forward = t;
        await r.signOut();
        await r.signInStaff(ANALYST, "/staff/fraud", "Fraud alerts");
        return ["PASS", `status=${t.status}`];
      });
    }

    await step("F6 forward held with MULE_PASS_THROUGH -> reject -> declined", async () => {
      const t = held.forward;
      if (t === null) throw new Error("no forward transfer");
      if (t.status !== "PENDING_REVIEW") throw new Error(`forward is ${t.status}, not held`);
      await r.say("The analyst opens the mule alert: MULE_PASS_THROUGH lists the inbound");
      const alert = await r.openAlertFor(t);
      if (!codes(alert).includes("MULE_PASS_THROUGH")) {
        throw new Error(`no MULE_PASS_THROUGH reason: ${codes(alert).join(",")}`);
      }
      const status = await r.decide(alert, "Reject", "mule pattern");
      if (status !== "DECLINED") throw new Error(`after REJECT the forward is ${status}`);
      return ["PASS", `score=${alert.risk_score} reasons=${codes(alert).join(",")} -> ${status}`];
    });

    // ----------------------------------------------------------------- 5. the assistant
    await r.say(
      "Back to Alice: Ask PBIP, the assistant (read-only tools, server-side answers)",
      1000,
    );
    await r.signOut();
    await r.signInCustomer(ALICE, true);
    await page.getByRole("button", { name: "Ask the PBIP assistant" }).click();
    const drawer = page.getByRole("dialog", { name: "PBIP assistant" });
    await expect(drawer).toBeVisible();
    await r.pause(1500);
    for (const q of QUESTIONS) {
      await step(`assistant: ${q.text}`, async () => {
        const caption =
          q.want === "refuse"
            ? "Another customer's data: refused by the guard, before any model is called"
            : q.want === "llm"
              ? "An unclear question goes to the language model, which only picks a tool"
              : "A clear question goes straight to a read-only tool";
        await r.say(caption, 1000);
        const before = await drawer.locator(".assistant-msg--assistant").count();
        await drawer.getByRole("textbox", { name: "Message" }).pressSequentially(q.text, {
          delay: 25,
        });
        await r.pause(500);
        const started = Date.now();
        const streamed = page.waitForResponse(
          (res) =>
            res.request().method() === "POST" && pathOf(res).startsWith("/api/v1/assistant/chat"),
        );
        await drawer.getByRole("button", { name: "Send" }).click();
        const response = await streamed;
        expect(response.status(), "assistant chat").toBe(200);
        const reply = drawer.locator(".assistant-msg--assistant").nth(before);
        await expect(reply).not.toHaveAttribute("aria-busy", "true", {
          timeout: 30_000,
        });
        await expect(drawer.getByRole("textbox", { name: "Message" })).toBeEnabled();
        const ms = Date.now() - started;
        const text = ((await reply.textContent()) ?? "").trim();
        const engine = /"engine":\s*"(\w+)"/.exec(await response.text().catch(() => ""))?.[1];
        await r.pause(3500);
        const summary = `${ms} ms in the UI, engine=${engine ?? "?"} :: ${JSON.stringify(text.slice(0, 80))}`;
        if (text === "") throw new Error(`empty reply (${summary})`);
        if (q.want === "refuse" && !text.includes("only help with your own")) {
          throw new Error(`not refused (${summary})`);
        }
        if (q.want === "llm" && engine !== undefined && engine !== "llm") {
          // demo-script.md section 7: Groq down or rate-limited falls back to Ollama, then rules.
          return ["DIFF", `engine=${engine}, not llm (documented fallback, section 7) ${summary}`];
        }
        return ["PASS", summary];
      });
    }
    await drawer.getByRole("button", { name: "Close assistant" }).click();

    // -------------------------------------------------------- 6. compliance hash-chain verify
    await r.say("The compliance officer verifies the tamper-evident audit log", 1000);
    await r.signOut();
    await step("compliance hash-chain verify", async () => {
      await r.signInStaff(COMPLIANCE, "/staff/audit-log", "Compliance audit log");
      const verified = page.waitForResponse(
        (res) => res.request().method() === "GET" && pathOf(res).endsWith("/audit-logs/verify"),
      );
      await page.getByRole("button", { name: "Verify hash chain" }).click();
      const body = (await (await verified).json()) as {
        valid: boolean;
        checked: number;
      };
      await expect(page.getByText("Hash chain intact.")).toBeVisible();
      await r.say(`Hash chain intact: ${body.checked} audit entries verified`, 4000);
      if (!body.valid) throw new Error("the hash chain is not valid");
      return ["PASS", `valid=${body.valid} checked=${body.checked}`];
    });
    await r.say("End of the PBIP demo", 3000);
  } catch (error) {
    failed = error;
  } finally {
    const video = page.video();
    await context.close();
    const name = `pbip-demo-${stamp}${failed ? "-FAILED" : ""}`;
    const webm = join(outDir, `${name}.webm`);
    if (video) {
      await video.saveAs(webm);
      await video.delete(); // the copy under .raw-<stamp>/ with Playwright's random name
      rmSync(join(outDir, `.raw-${stamp}`), { recursive: true, force: true });
    }
    const mp4 = toMp4(webm);
    writeFileSync(
      join(outDir, `${name}.json`),
      `${JSON.stringify({ video: mp4 ?? webm, results: r.results }, null, 2)}\n`,
    );
    const passed = r.results.filter((s) => s.outcome !== "FAIL").length;
    console.log(`\nVIDEO ${mp4 ?? webm}\nSUMMARY ${passed}/${r.results.length} steps without FAIL`);
    console.log(`SIGN-IN REQUESTS ${r.signInRequests}`);
  }
  if (failed) throw failed;
});

/** H.264 MP4 next to the WebM when ffmpeg is on PATH (Playwright's own ffmpeg writes WebM only). */
function toMp4(webm: string): string | null {
  const mp4 = webm.replace(/\.webm$/, ".mp4");
  try {
    execFileSync(
      "ffmpeg",
      [
        "-y",
        "-loglevel",
        "error",
        "-i",
        webm,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "20",
        mp4,
      ],
      { stdio: ["ignore", "ignore", "inherit"] },
    );
    return mp4;
  } catch {
    return null;
  }
}
