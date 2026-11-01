import { useQuery } from "@tanstack/react-query";
import { api, apiFetchAs, ApiError } from "../../api/client";
import type {
  AccountOut,
  AlertDetail,
  CustomerProfileOut,
  PayeeOut,
  PayeeVerificationOut,
  TokenResponse,
  TransferOut,
} from "../../api/client";
import { errorMessage } from "../../lib/errors";

/**
 * TASK-081 fraud demonstration lab.
 *
 * Every scenario is a sequence of REAL calls to the customer API (sign-in, payees, devices,
 * transfers) made as a seeded demo customer, followed by the analyst's own fraud API to read the
 * alert the engine raised for that exact transfer. Nothing is scored or decided in the browser:
 * the decision, score and reasons shown are the ones the server persisted. Money only moves
 * through POST /transfers (ledger, limits, idempotency and the fraud scorer all apply).
 */

export type ScenarioCode = "F1" | "F2" | "F3" | "F4" | "F5" | "F6";

export interface Scenario {
  code: ScenarioCode;
  title: string;
  summary: string;
  /** The API calls the lab makes, in order, as plain sentences. */
  sequence: string[];
  /** Engine signals this sequence can trip (rule codes in modules/fraud/baseline.py). */
  signals: string;
  /** Why a scenario cannot be driven honestly from a browser; it then has no Run button. */
  notRunnable?: string;
}

export const LAB_PAYEE_NAME = "PBIP Lab Merchant";
const LAB_PAYEE_ACCOUNT = "LAB000081";
/** Payees outside PBIP settle through the simulated clearing account; names are not checked. */
const EXTERNAL_BANK = "EXTB0001";
const INTERNAL_BANK = "PBIP0001";
/** The demo customer's "usual" device: registered before the takeover so a new one stands out. */
const USUAL_DEVICE_ID = "pbip-lab-usual-device";
const MULE_INBOUND_MINOR = 2_400_000; // 24,000.00: below the 25,000.00 new-payee threshold

export const SCENARIOS: Scenario[] = [
  {
    code: "F1",
    title: "APP scam",
    summary:
      "A customer is talked into paying a brand-new payee a large sum straight after adding it.",
    sequence: [
      "Sign in as the customer",
      "Add and verify a new external payee",
      "Transfer INR 30,000.00 to it",
    ],
    signals: "NEW_PAYEE_LARGE_AMOUNT (payee under 24 hours old, amount of 25,000.00 or more)",
  },
  {
    code: "F2",
    title: "Velocity burst",
    summary: "Several transfers leave the account within seconds.",
    sequence: [
      "Sign in as the customer",
      `Use the "${LAB_PAYEE_NAME}" payee (created once)`,
      "Send four transfers of INR 500.00 back to back",
    ],
    signals: "VELOCITY_BURST (4 or more transfers from the account within 10 minutes)",
  },
  {
    code: "F3",
    title: "Account takeover",
    summary:
      "Someone guesses the password, gets in, and moves money from a device the customer never used.",
    sequence: [
      "Sign in as the customer and register their usual device",
      "Three sign-ins with a wrong password",
      "Sign in with the right password and transfer INR 50,000.00 from a new device",
    ],
    signals:
      "Failed sign-ins in 24 hours, NEW_DEVICE (a device not seen before), plus the payee and amount rules",
  },
  {
    code: "F4",
    title: "Impossible travel",
    summary: "A transfer comes from a country the customer has never transacted from.",
    sequence: [],
    signals: "NEW_COUNTRY (needs the GeoIP database, ADR-0014)",
    notRunnable:
      "The country comes from the network address the server sees. A browser cannot change that " +
      "honestly (the lab does not forge X-Forwarded-For), and without GEOIP_COUNTRY_DB_PATH the " +
      "server records no country at all. The rule is covered by the API integration tests.",
  },
  {
    code: "F5",
    title: "Amount anomaly",
    summary: "One transfer far larger than the account's usual transfers.",
    sequence: [
      "Sign in as the customer",
      `Transfer INR 1,00,000.00 to the "${LAB_PAYEE_NAME}" payee`,
    ],
    signals:
      "LARGE_AMOUNT (1,00,000.00 or more) and AMOUNT_SPIKE (5x the 90-day average, once the account has 3 posted transfers, e.g. after F2)",
  },
  {
    code: "F6",
    title: "Mule pass-through",
    summary:
      "Money arrives from another customer and is forwarded straight on to a new outside account.",
    sequence: [
      "Sign in as the sender and as the mule customer",
      "Sender pays the mule INR 24,000.00 (a real internal transfer)",
      "Mule adds a new external payee and forwards 95% of what arrived",
    ],
    signals:
      "No dedicated pass-through signal exists yet (Blueprint F6); the forward is scored only on " +
      "the new payee and amount, so the engine may allow it. The lab shows what it decides.",
  },
];

export interface Credentials {
  email: string;
  password: string;
}

export type StepState = "running" | "done" | "failed";

export interface LabStep {
  id: number;
  label: string;
  state: StepState;
  detail?: string;
}

export interface LabTransfer {
  label: string;
  transfer: TransferOut;
  /** The analyst's view of the alert raised for this transfer; null when none was raised. */
  alert: AlertDetail | null;
}

export interface LabRun {
  code: ScenarioCode;
  transfers: LabTransfer[];
}

export interface RunInput {
  code: ScenarioCode;
  customer: Credentials;
  /** F6 only: the customer who sends the money in. */
  sender: Credentials;
  onProgress: (steps: LabStep[]) => void;
}

/** A failure the lab explains itself (as opposed to an API error). */
export class LabError extends Error {}

/** The server decides whether the lab may run (false in production); see GET /fraud/demo-lab. */
export function useDemoLabStatus(enabled: boolean) {
  return useQuery({
    queryKey: ["fraud", "demo-lab"],
    queryFn: () => api.fraudDemoLab(),
    enabled,
    staleTime: Infinity,
  });
}

class Progress {
  private steps: LabStep[] = [];

  constructor(private readonly emit: (steps: LabStep[]) => void) {}

  async step<T>(label: string, work: () => Promise<T>): Promise<T> {
    const id = this.steps.length + 1;
    this.steps = [...this.steps, { id, label, state: "running" }];
    this.emit(this.steps);
    try {
      const result = await work();
      this.set(id, { state: "done" });
      return result;
    } catch (error) {
      const detail =
        error instanceof LabError ? error.message : errorMessage(error, "The request failed.");
      this.set(id, { state: "failed", detail });
      throw error;
    }
  }

  private set(id: number, patch: Partial<LabStep>): void {
    this.steps = this.steps.map((s) => (s.id === id ? { ...s, ...patch } : s));
    this.emit(this.steps);
  }
}

function post(body?: unknown, headers?: Record<string, string>): RequestInit {
  return {
    method: "POST",
    ...(headers ? { headers } : {}),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

async function signIn(who: Credentials): Promise<string> {
  const body = await apiFetchAs<TokenResponse>(
    null,
    "/api/v1/auth/login",
    post({ email: who.email, password: who.password }),
  );
  if (body.mfa_required || !body.access_token) {
    throw new LabError(
      `${who.email} has an authenticator enrolled; the lab signs in demo customers without MFA only.`,
    );
  }
  return body.access_token;
}

/** A sign-in with a wrong password. Only the expected 401 counts; a lockout or rate limit stops. */
async function failedSignIn(email: string, attempt: number): Promise<void> {
  try {
    await apiFetchAs<TokenResponse>(
      null,
      "/api/v1/auth/login",
      post({ email, password: `wrong-password-attempt-${String(attempt)}` }),
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    throw error;
  }
  throw new LabError("A wrong password was accepted; check the demo data.");
}

async function primaryAccount(token: string): Promise<AccountOut> {
  const accounts = await apiFetchAs<AccountOut[]>(token, "/api/v1/accounts");
  const account = accounts.find((a) => a.status === "ACTIVE");
  if (!account) throw new LabError("This customer has no active account to pay from.");
  return account;
}

function randomAccountNumber(prefix: string): string {
  return prefix + crypto.randomUUID().replace(/-/g, "").slice(0, 12).toUpperCase();
}

async function verify(token: string, payee: PayeeOut): Promise<PayeeOut> {
  const result = await apiFetchAs<PayeeVerificationOut>(
    token,
    `/api/v1/payees/${encodeURIComponent(payee.id)}/verify`,
    post(),
  );
  if (result.payee.status !== "VERIFIED") {
    throw new LabError(`The payee check returned ${result.name_match}; nothing was sent.`);
  }
  return result.payee;
}

async function addPayee(
  token: string,
  name: string,
  accountNumber: string,
  bankCode: string,
): Promise<PayeeOut> {
  const created = await apiFetchAs<PayeeOut>(
    token,
    "/api/v1/payees",
    post({ name, account_number: accountNumber, bank_code: bankCode }),
  );
  return verify(token, created);
}

/** Reuses a payee the customer already has (same name, bank and last four digits). */
async function payeeFor(
  token: string,
  name: string,
  accountNumber: string,
  bankCode: string,
): Promise<PayeeOut> {
  const existing = (await apiFetchAs<PayeeOut[]>(token, "/api/v1/payees")).find(
    (p) =>
      p.name === name &&
      p.bank_code === bankCode &&
      p.account_number_masked.endsWith(accountNumber.slice(-4)),
  );
  if (!existing) return addPayee(token, name, accountNumber, bankCode);
  return existing.status === "VERIFIED" ? existing : verify(token, existing);
}

async function transfer(
  token: string,
  from: AccountOut,
  payee: PayeeOut,
  amountMinor: number,
  reference: string,
  deviceId?: string,
): Promise<TransferOut> {
  const headers: Record<string, string> = { "Idempotency-Key": `lab-${crypto.randomUUID()}` };
  if (deviceId) headers["X-Device-ID"] = deviceId;
  return apiFetchAs<TransferOut>(
    token,
    "/api/v1/transfers",
    post(
      {
        from_account_id: from.id,
        payee_id: payee.id,
        amount_minor: amountMinor,
        reference,
      },
      headers,
    ),
  );
}

/** The analyst's own view of what the engine decided for this exact transfer. */
async function alertFor(transfer: TransferOut): Promise<AlertDetail | null> {
  const [alert] = await api.fraudAlerts(undefined, transfer.id);
  return alert ? api.fraudAlert(alert.id) : null;
}

type Runner = (input: RunInput, progress: Progress) => Promise<LabTransfer[]>;

async function sendAndInspect(
  progress: Progress,
  label: string,
  send: () => Promise<TransferOut>,
): Promise<LabTransfer> {
  const sent = await progress.step(label, send);
  const alert = await progress.step("Look up the fraud alert for this transfer", () =>
    alertFor(sent),
  );
  return { label, transfer: sent, alert };
}

const appScam: Runner = async ({ customer }, progress) => {
  const token = await progress.step(`Sign in as ${customer.email}`, () => signIn(customer));
  const from = await progress.step("Load the customer's account", () => primaryAccount(token));
  const payee = await progress.step("Add and verify a brand-new external payee", () =>
    addPayee(token, "Urgent Investments", randomAccountNumber("SCAM"), EXTERNAL_BANK),
  );
  return [
    await sendAndInspect(progress, "Transfer INR 30,000.00 to the new payee", () =>
      transfer(token, from, payee, 3_000_000, "Lab F1 APP scam"),
    ),
  ];
};

const velocityBurst: Runner = async ({ customer }, progress) => {
  const token = await progress.step(`Sign in as ${customer.email}`, () => signIn(customer));
  const from = await progress.step("Load the customer's account", () => primaryAccount(token));
  const payee = await progress.step(`Find or add the "${LAB_PAYEE_NAME}" payee`, () =>
    payeeFor(token, LAB_PAYEE_NAME, LAB_PAYEE_ACCOUNT, EXTERNAL_BANK),
  );
  const results: LabTransfer[] = [];
  for (let n = 1; n <= 4; n += 1) {
    results.push(
      await sendAndInspect(progress, `Transfer ${String(n)} of 4: INR 500.00`, () =>
        transfer(token, from, payee, 50_000, `Lab F2 burst ${String(n)}`),
      ),
    );
  }
  return results;
};

const accountTakeover: Runner = async ({ customer }, progress) => {
  const owner = await progress.step(`Sign in as ${customer.email} (the real customer)`, () =>
    signIn(customer),
  );
  await progress.step("Register the customer's usual device", () =>
    apiFetchAs(
      owner,
      "/api/v1/customers/devices",
      post({ label: "Lab: customer's usual device" }, { "X-Device-ID": USUAL_DEVICE_ID }),
    ),
  );
  for (let n = 1; n <= 3; n += 1) {
    await progress.step(`Attacker sign-in ${String(n)} of 3 with a wrong password`, () =>
      failedSignIn(customer.email, n),
    );
  }
  const attacker = await progress.step("Attacker signs in with the stolen password", () =>
    signIn(customer),
  );
  const from = await progress.step("Load the customer's account", () => primaryAccount(attacker));
  const payee = await progress.step(`Find or add the "${LAB_PAYEE_NAME}" payee`, () =>
    payeeFor(attacker, LAB_PAYEE_NAME, LAB_PAYEE_ACCOUNT, EXTERNAL_BANK),
  );
  const newDevice = `lab-attacker-${crypto.randomUUID()}`;
  return [
    await sendAndInspect(progress, "Transfer INR 50,000.00 from a new device", () =>
      transfer(attacker, from, payee, 5_000_000, "Lab F3 takeover", newDevice),
    ),
  ];
};

const amountAnomaly: Runner = async ({ customer }, progress) => {
  const token = await progress.step(`Sign in as ${customer.email}`, () => signIn(customer));
  const from = await progress.step("Load the customer's account", () => primaryAccount(token));
  const payee = await progress.step(`Find or add the "${LAB_PAYEE_NAME}" payee`, () =>
    payeeFor(token, LAB_PAYEE_NAME, LAB_PAYEE_ACCOUNT, EXTERNAL_BANK),
  );
  return [
    await sendAndInspect(progress, "Transfer INR 1,00,000.00", () =>
      transfer(token, from, payee, 10_000_000, "Lab F5 amount anomaly"),
    ),
  ];
};

const mulePassThrough: Runner = async ({ customer, sender }, progress) => {
  if (sender.email.trim().toLowerCase() === customer.email.trim().toLowerCase()) {
    throw new LabError("The sender and the mule must be different customers.");
  }
  const senderToken = await progress.step(`Sign in as the sender ${sender.email}`, () =>
    signIn(sender),
  );
  const muleToken = await progress.step(`Sign in as the mule ${customer.email}`, () =>
    signIn(customer),
  );
  const muleAccount = await progress.step("Load the mule's account", () =>
    primaryAccount(muleToken),
  );
  const muleProfile = await progress.step("Load the mule's name for the payee check", () =>
    apiFetchAs<CustomerProfileOut>(muleToken, "/api/v1/customers/me"),
  );
  const senderAccount = await progress.step("Load the sender's account", () =>
    primaryAccount(senderToken),
  );
  const toMule = await progress.step("Sender finds or adds the mule as a payee", () =>
    payeeFor(senderToken, muleProfile.full_name, muleAccount.account_number, INTERNAL_BANK),
  );
  const inbound = await sendAndInspect(progress, "Sender pays the mule INR 24,000.00", () =>
    transfer(senderToken, senderAccount, toMule, MULE_INBOUND_MINOR, "Lab F6 inbound"),
  );
  if (inbound.transfer.status !== "POSTED") {
    return [inbound]; // nothing arrived, so there is nothing to forward
  }
  const outside = await progress.step("Mule adds a new external payee", () =>
    addPayee(muleToken, "Quick Cash Exports", randomAccountNumber("MULE"), EXTERNAL_BANK),
  );
  const forward = Math.floor((MULE_INBOUND_MINOR * 95) / 100);
  const outbound = await sendAndInspect(progress, "Mule forwards 95% (INR 22,800.00)", () =>
    transfer(muleToken, muleAccount, outside, forward, "Lab F6 forward"),
  );
  return [inbound, outbound];
};

const RUNNERS: Partial<Record<ScenarioCode, Runner>> = {
  F1: appScam,
  F2: velocityBurst,
  F3: accountTakeover,
  F5: amountAnomaly,
  F6: mulePassThrough,
};

export function isRunnable(code: ScenarioCode): boolean {
  return RUNNERS[code] !== undefined;
}

/** Runs one scenario against the live API. Rejects on the first failed step (shown in progress). */
export async function runScenario(input: RunInput): Promise<LabRun> {
  const runner = RUNNERS[input.code];
  if (!runner) throw new LabError(`${input.code} cannot be run from the browser.`);
  const transfers = await runner(input, new Progress(input.onProgress));
  return { code: input.code, transfers };
}
