import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import type { BrowserContext, Page, Request, Response } from "@playwright/test";
import { enterSignInCode, signIn, staffCode } from "../../apps/web/e2e/support";

// The machinery of the A-to-Z walkthrough recording (record-walkthrough.ts): the timeline the
// editor (edit-walkthrough.py) cuts the raw video by, the caption overlay, the sign-in budget and
// the API checks. Nothing here changes what the app does: overlays are aria-hidden with
// pointer-events: none, and the MFA key and recovery codes are blurred with inline styles set
// through the CSSOM (the app's CSP allows that; it forbids injected <style> elements).

export type Outcome = "PASS" | "DIFF" | "PENDING" | "FAIL";

/** A span of the raw video, in seconds from the start of the recording. */
export interface Mark {
  kind: "dead" | "fast";
  s: number;
  e: number;
}

export interface TimelineStep {
  chapter: number;
  name: string;
  start: number;
  end: number;
  outcome: Outcome;
  detail: string;
  highlight: boolean;
  /** Set when the page used a smaller viewport (the phone layout): the editor crops the top-left
   * width x height of the frame and centres it, enlarged, on the card colour. */
  frame?: { width: number; height: number };
  marks: Mark[];
}

export interface Timeline {
  version: 1;
  video: string;
  size: { width: number; height: number };
  recorded_at: string;
  chapters: { title: string; highlight: boolean }[];
  steps: TimelineStep[];
  captions: { t: number; text: string }[];
  sign_in_requests: number;
}

/** Sign-in and MFA verify share 10 requests per minute per IP; stay one below it. */
const SIGN_IN_BUDGET = 9;
/** DEMO_SKIP: step names (|-separated) not to record, when another take is merged in for them. */
const SKIP = (process.env.DEMO_SKIP ?? "").split("|").filter((n) => n !== "");
/** The shortest a feature may be on screen in the edited film (the owner's 10-20 seconds). */
const MIN_STEP_SECONDS = Number(process.env.DEMO_MIN_STEP_SECONDS ?? "10");
const SIGN_IN_PATHS = new Set(["/api/v1/auth/login", "/api/v1/auth/mfa/verify"]);
/** What the blur hides: the authenticator key, its otpauth link and the recovery codes. */
const SECRET_SELECTOR = 'code.secret, ul.recovery-codes, a[href^="otpauth:"]';

export function pathOf(r: Response | Request): string {
  return new URL(r.url()).pathname;
}

/** The documented local demo password: PBIP_DEMO_PASSWORD if set, else the APP_ENV=local constant
 * in scripts/seed_dev.py (demo-script.md section 1). Never logged. */
export function demoPassword(repoRoot: string): string {
  const fromEnv = process.env.PBIP_DEMO_PASSWORD;
  if (fromEnv) return fromEnv;
  const seed = readFileSync(join(repoRoot, "scripts/seed_dev.py"), "utf8");
  const constant = /^DEMO_PASSWORD = "([^"]+)"/m.exec(seed)?.[1];
  if (!constant)
    throw new Error("set PBIP_DEMO_PASSWORD: no DEMO_PASSWORD constant in seed_dev.py");
  return constant;
}

/** Grafana's admin password: GRAFANA_ADMIN_PASSWORD if set, else the running Grafana container's
 * own environment (docker-compose.yml passes it in). Never logged. */
export function grafanaPassword(): string {
  const fromEnv = process.env.GRAFANA_ADMIN_PASSWORD;
  if (fromEnv) return fromEnv;
  const container = process.env.DEMO_GRAFANA_CONTAINER ?? "pbip-grafana-1";
  const value = execFileSync(
    "docker",
    ["exec", container, "printenv", "GF_SECURITY_ADMIN_PASSWORD"],
    {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    },
  ).trim();
  if (!value) throw new Error("no Grafana admin password in the environment or the container");
  return value;
}

/** Installed in every document before the app's own scripts: keeps the secrets blurred from the
 * first frame they are painted (a MutationObserver callback runs before the next render). */
function installBlur(selector: string): void {
  const blur = (root: ParentNode) => {
    root.querySelectorAll<HTMLElement>(selector).forEach((el) => {
      el.style.setProperty("filter", "blur(9px)", "important");
      el.style.setProperty("user-select", "none");
    });
  };
  const start = () => {
    blur(document);
    new MutationObserver(() => {
      blur(document);
    }).observe(document.documentElement, { childList: true, subtree: true });
  };
  if (document.documentElement) start();
  else document.addEventListener("DOMContentLoaded", start);
}

export class Walkthrough {
  readonly t0: number;
  readonly chapters: Timeline["chapters"] = [];
  readonly steps: TimelineStep[] = [];
  readonly captions: Timeline["captions"] = [];
  private caption = "";
  private signIns: number[] = [];
  private current: TimelineStep | null = null;
  signInRequests = 0;
  /** The bearer token the app last sent, for read-only checks of what the page shows. */
  bearer = "";
  /** Server errors seen on the API: any 5xx fails the step that caused it. */
  private serverErrors: string[] = [];

  constructor(
    readonly page: Page,
    readonly password: string,
    readonly pace: number,
    readonly captionsOn: boolean,
  ) {
    this.t0 = Date.now();
    page.on("request", (request) => {
      const path = pathOf(request);
      if (request.method() === "POST" && SIGN_IN_PATHS.has(path)) {
        this.signIns.push(Date.now());
        this.signInRequests += 1;
      }
      const auth = request.headers()["authorization"];
      if (path.startsWith("/api/v1/") && auth?.startsWith("Bearer ")) this.bearer = auth;
    });
    page.on("response", (response) => {
      if (pathOf(response).startsWith("/api/v1/") && response.status() >= 500) {
        this.serverErrors.push(
          `${response.request().method()} ${pathOf(response)} ${response.status()}`,
        );
      }
    });
  }

  static async prepare(context: BrowserContext): Promise<void> {
    await context.addInitScript(installBlur, SECRET_SELECTOR);
  }

  /** Seconds since the recording started (the raw video's clock). */
  now(): number {
    return (Date.now() - this.t0) / 1000;
  }

  // ---------------------------------------------------------------------------- the timeline

  chapter(title: string, highlight = false): void {
    this.chapters.push({ title, highlight });
  }

  /** One feature: its body returns the outcome and a one-line detail (statuses, counts; never a
   * credential). A thrown error is recorded as FAIL and stops the run. */
  async step(
    name: string,
    opts: { highlight?: boolean; frame?: { width: number; height: number } },
    body: () => Promise<[Outcome, string]>,
  ): Promise<void> {
    if (SKIP.includes(name)) return; // DEMO_SKIP: a step another take already holds
    const entry: TimelineStep = {
      chapter: this.chapters.length - 1,
      name,
      start: this.now(),
      end: 0,
      outcome: "PASS",
      detail: "",
      highlight: opts.highlight ?? false,
      ...(opts.frame ? { frame: opts.frame } : {}),
      marks: [],
    };
    this.current = entry;
    this.serverErrors = [];
    try {
      const [outcome, detail] = await test.step(name, body);
      if (this.serverErrors.length > 0)
        throw new Error(`server error: ${this.serverErrors.join(", ")}`);
      entry.outcome = outcome;
      entry.detail = detail;
      // Every feature gets at least MIN_STEP_SECONDS on screen after editing: the rest of it is
      // spent on the result, which is what the viewer came to see.
      const shown = this.shownSeconds(entry);
      if (shown < MIN_STEP_SECONDS) {
        await this.page.waitForTimeout(Math.round((MIN_STEP_SECONDS - shown) * 1000 * this.pace));
      }
    } catch (error) {
      entry.outcome = "FAIL";
      entry.detail = error instanceof Error ? (error.message.split("\n")[0] ?? "") : String(error);
      throw error;
    } finally {
      entry.end = this.now();
      this.current = null;
      this.steps.push(entry);
      console.log(`${entry.outcome} ${name} :: ${entry.detail}`);
    }
  }

  /** How long a step will last after editing, so far: dead marks cut, fast marks at 1.5x. */
  private shownSeconds(entry: TimelineStep): number {
    const total = this.now() - entry.start;
    const dead = entry.marks.filter((m) => m.kind === "dead").reduce((a, m) => a + m.e - m.s, 0);
    const fast = entry.marks.filter((m) => m.kind === "fast").reduce((a, m) => a + m.e - m.s, 0);
    return total - dead - fast / 3; // a fast second plays in 2/3 s (nested marks: a close bound)
  }

  /** A feature that is not on this build yet: recorded as PENDING with no video time. */
  pending(name: string, why: string): void {
    const t = this.now();
    this.steps.push({
      chapter: this.chapters.length - 1,
      name,
      start: t,
      end: t,
      outcome: "PENDING",
      detail: why,
      highlight: false,
      marks: [],
    });
    console.log(`PENDING ${name} :: ${why}`);
  }

  private async marked<T>(kind: Mark["kind"], fn: () => Promise<T>): Promise<T> {
    const s = this.now();
    try {
      return await fn();
    } finally {
      this.current?.marks.push({ kind, s, e: this.now() });
    }
  }

  /** Waiting the viewer does not need to see (network, page loads, codes, rate limit): cut. */
  dead<T>(fn: () => Promise<T>): Promise<T> {
    return this.marked("dead", fn);
  }

  /** Something worth showing but slow (forms filling, a list scrolling): played at 1.5x. */
  fast<T>(fn: () => Promise<T>): Promise<T> {
    return this.marked("fast", fn);
  }

  // ------------------------------------------------------------------------------ on screen

  async hold(ms: number): Promise<void> {
    await this.page.waitForTimeout(Math.round(ms * this.pace));
  }

  /** Caption overlay plus a timeline entry (the editor turns those into the .srt). */
  async say(text: string, holdMs = 0): Promise<void> {
    this.caption = text;
    this.captions.push({ t: this.now(), text });
    await this.showCaption();
    if (holdMs > 0) await this.hold(holdMs);
  }

  async showCaption(): Promise<void> {
    if (!this.captionsOn || this.caption === "") return;
    await this.page.evaluate((text) => {
      const id = "pbip-demo-caption";
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement("div");
        el.id = id;
        el.setAttribute("aria-hidden", "true");
        Object.assign(el.style, {
          // Bottom centre: the app keeps its actions at the top and in the page body.
          position: "fixed",
          bottom: "22px",
          left: "50%",
          transform: "translateX(-50%)",
          maxWidth: "70vw",
          textAlign: "center",
          zIndex: "2147483647",
          pointerEvents: "none",
          background: "rgba(15, 23, 42, 0.92)",
          color: "#ffffff",
          font: "600 24px/1.35 system-ui, -apple-system, 'Segoe UI', sans-serif",
          padding: "10px 22px",
          borderRadius: "10px",
          boxShadow: "0 6px 24px rgba(0, 0, 0, 0.35)",
        });
        document.body.appendChild(el);
      }
      // Smaller on the phone viewport, where the overlay would otherwise cover the page.
      const narrow = window.innerWidth < 600;
      el.style.font = `600 ${narrow ? 15 : 24}px/1.35 system-ui, -apple-system, 'Segoe UI', sans-serif`;
      el.style.maxWidth = narrow ? "92vw" : "70vw";
      el.style.bottom = narrow ? "76px" : "22px";
      el.textContent = text;
    }, this.caption);
  }

  /** In-app navigation by URL: the router follows a pushState + popstate exactly as it follows a
   * link, so the session and the overlays stay (a full reload would re-fetch everything). */
  async go(path: string, heading: string | RegExp): Promise<void> {
    await this.dead(async () => {
      await this.page.evaluate((to) => {
        window.history.pushState({}, "", to);
        window.dispatchEvent(new PopStateEvent("popstate"));
      }, path);
      await expect(
        this.page
          .getByRole("heading", {
            name: heading,
            exact: typeof heading === "string",
          })
          .first(),
      ).toBeVisible();
      await this.page.evaluate(() => {
        window.scrollTo(0, 0);
      });
    });
  }

  /** Smooth scroll to a locator's heading so the viewer sees where we are going. */
  async reveal(selector: ReturnType<Page["getByRole"]>): Promise<void> {
    await selector.first().evaluate((el) => {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    await this.hold(700);
  }

  /** A 1920x1080 PNG for the docs (DEMO_STILLS_DIR), without the caption overlay. Cut. */
  async still(name: string): Promise<void> {
    const dir = process.env.DEMO_STILLS_DIR;
    if (!dir) return;
    await this.dead(async () => {
      const overlay = this.page.locator("#pbip-demo-caption");
      const shown = (await overlay.count()) > 0;
      if (shown) await overlay.evaluate((el) => el.style.setProperty("display", "none"));
      await this.page.waitForTimeout(300);
      await this.page.screenshot({ path: `${dir}/${name}.png` });
      if (shown) await overlay.evaluate((el) => el.style.removeProperty("display"));
    });
  }

  // ---------------------------------------------------------------------------- API checks

  /** Runs `action` and returns the API response it triggers (method + path), checked against
   * `want` (default: a 2xx). The response is read from the browser's own traffic. */
  async api<T>(
    method: string,
    path: string | RegExp,
    action: () => Promise<unknown>,
    want: (status: number) => boolean = (s) => s >= 200 && s < 300,
  ): Promise<{ status: number; body: T }> {
    const matches = (p: string) => (typeof path === "string" ? p === path : path.test(p));
    const waited = this.page.waitForResponse(
      (r) => r.request().method() === method && matches(pathOf(r)),
      { timeout: 60_000 },
    );
    await action();
    const response = await this.dead(() => waited);
    const status = response.status();
    if (!want(status)) throw new Error(`${method} ${pathOf(response)} answered ${status}`);
    const text = await response.text().catch(() => "");
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status, body: body as T };
  }

  /** A read-only API call with the bearer the app last used. */
  async apiGet<T>(path: string, want = 200): Promise<T> {
    const response = await this.page.request.get(path, {
      headers: { Authorization: this.bearer },
    });
    expect(response.status(), `GET ${path}`).toBe(want);
    return (await response.json()) as T;
  }

  // ------------------------------------------------------------------------------ sign-in

  /** Waits (cut from the video) so the next sign-in stays inside the per-IP rate limit. */
  async signInBudget(requests: number): Promise<void> {
    await this.dead(async () => {
      for (;;) {
        const now = Date.now();
        this.signIns = this.signIns.filter((t) => now - t < 61_000);
        if (this.signIns.length + requests <= SIGN_IN_BUDGET) return;
        const oldest = this.signIns[0] ?? now;
        await this.page.waitForTimeout(61_000 - (now - oldest) + 250);
      }
    });
  }

  async signOut(): Promise<void> {
    await this.dead(async () => {
      await this.page.getByRole("button", { name: "Sign out" }).click();
      await expect(this.page.getByRole("button", { name: "Sign in" })).toBeVisible();
    });
    this.bearer = "";
  }

  /** Password, then the authenticator code (the seeded user's own, read with scripts/totp.py). */
  async signInWithCode(email: string, path: string, heading: string): Promise<void> {
    await this.signInBudget(2);
    await this.api("POST", "/api/v1/auth/login", () =>
      signIn(this.page, email, this.password, path),
    );
    await expect(this.page.getByLabel("Authenticator or recovery code")).toBeVisible();
    await this.showCaption();
    await this.hold(900);
    const code = await this.dead(() => Promise.resolve(staffCode(email)));
    await this.api("POST", "/api/v1/auth/mfa/verify", () => enterSignInCode(this.page, code));
    await this.dead(() =>
      expect(this.page.getByRole("heading", { name: heading, exact: true })).toBeVisible(),
    );
    await this.showCaption();
  }
}
