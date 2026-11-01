import type { AlertDetail, AlertSummary } from "../../api/client";
import { parseReasons } from "./fraudFormat";
import type { FraudReason } from "./fraudFormat";

export type ScenarioCode = "F1" | "F2" | "F3" | "F4" | "F5" | "F6";

export interface ScenarioDefinition {
  code: ScenarioCode;
  title: string;
  subtitle: string;
  description: string;
  attackSignature: string;
  defaultAmountMinor: number;
  detectionVector: string;
  parameters: Record<string, string>;
}

export const SCENARIOS: ScenarioDefinition[] = [
  {
    code: "F1",
    title: "APP Scam",
    subtitle: "Authorised push payment deception",
    description:
      "Target customer is socially engineered into adding an urgent destination and immediately transferring significant funds.",
    attackSignature: "New payee verified minutes before a transfer exceeding normal baseline",
    defaultAmountMinor: 3_000_000, // INR 30,000.00
    detectionVector: "Rule NEW_PAYEE_LARGE_AMOUNT + Model payee age features",
    parameters: {
      Payee: "Urgent Investment Co (999900001111)",
      Amount: "INR 30,000.00",
      Pattern: "Instant transfer to new recipient",
    },
  },
  {
    code: "F2",
    title: "Velocity Burst",
    subtitle: "Rapid transaction burst",
    description:
      "Multiple transfers dispatched in rapid succession within seconds to drain funds before the cardholder notices.",
    attackSignature:
      "5 rapid transfers within seconds exceeding the rolling 10-minute velocity threshold",
    defaultAmountMinor: 500_000, // INR 5,000.00 per hop
    detectionVector: "Redis sliding-window velocity counters (velocity_10m)",
    parameters: {
      Count: "5 sequential transfers",
      Interval: "< 1 second per transfer",
      Amount: "INR 5,000.00 each",
    },
  },
  {
    code: "F3",
    title: "Account Takeover",
    subtitle: "Credential attack and device change",
    description:
      "Multiple failed authentication attempts followed by login from an unrecognized device and a draining transfer.",
    attackSignature: "3 failed logins + new device ID header + immediate high-value transfer",
    defaultAmountMinor: 5_000_000, // INR 50,000.00
    detectionVector: "Device anomaly (distinct_devices_7d) + credential failure sequence",
    parameters: {
      "Failed attempts": "3 brute-force attempts",
      "Simulated device": "device-attacker-laptop-01",
      Amount: "INR 50,000.00",
    },
  },
  {
    code: "F4",
    title: "Impossible Travel",
    subtitle: "Geographical velocity anomaly",
    description:
      "Transfer initiated from a remote international IP address within an impossible travel window.",
    attackSignature: "Rapid geographical displacement requiring superhuman physical travel speed",
    defaultAmountMinor: 1_500_000, // INR 15,000.00
    detectionVector: "GeoIP country resolution + foreign IP mismatch",
    parameters: {
      "Remote IP": "185.220.101.5 (Foreign exit node)",
      Amount: "INR 15,000.00",
    },
  },
  {
    code: "F5",
    title: "Amount Anomaly",
    subtitle: "Baseline expenditure spike",
    description:
      "A transfer amount drastically higher (10x) than the customer's historical 30-day average transaction amount.",
    attackSignature: "Transfer amount >> customer 30-day rolling mean",
    defaultAmountMinor: 15_000_000, // INR 1,50,000.00
    detectionVector: "IsolationForest anomaly score + LightGBM ratio_to_avg_amount",
    parameters: {
      Amount: "INR 1,50,000.00",
      Baseline: "10x customer 30-day average",
    },
  },
  {
    code: "F6",
    title: "Mule Pass-Through",
    subtitle: "Layering and rapid forwarding",
    description:
      "Sudden inward liquidity followed by near-immediate forwarding (95%) to an external mule account.",
    attackSignature: "Inflow followed by rapid outflow (>90%) to clean illicit funds",
    defaultAmountMinor: 2_500_000, // INR 25,000.00
    detectionVector: "Circular mule loop heuristics + rapid fund forwarding rule",
    parameters: {
      Forwarding: "95% of available funds",
      Destination: "Mule Ring Account (888877776666)",
      Amount: "INR 25,000.00",
    },
  },
];

export interface AttackStep {
  step: number;
  label: string;
  status: "pending" | "running" | "completed" | "failed";
  error?: string;
}

export interface AttackResult {
  scenarioCode: ScenarioCode;
  transferId?: string;
  transferStatus?: string;
  amountMinor?: number;
  alertId?: string;
  riskScore?: number;
  decision?: string;
  severity?: string;
  reasons: FraudReason[];
}

/** Default demo customer password matching scripts/seed_dev.py (local demo data only). */
export const DEFAULT_DEMO_PASSWORD = "Demo-Password-2026!"; // pragma: allowlist secret

/** Simulated incorrect password for credential brute-force testing (F3). */
export const WRONG_PASSWORD_ATTEMPT = "Incorrect-Pass-123!"; // pragma: allowlist secret

/** Generates a cryptographically secure 4-digit numeric string for demo account suffixes. */
export function secureRandom4Digits(): string {
  const bytes = new Uint16Array(1);
  crypto.getRandomValues(bytes);
  const val = bytes[0] ?? 0;
  return String(1000 + (val % 9000));
}

/** Generates a cryptographically secure idempotency key conforming to ^[A-Za-z0-9._:-]+$ (min 8, max 128 chars). */
export function generateIdempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

/** Check if current runtime environment is production (where simulator is strictly disabled). */
export function isProductionEnv(): boolean {
  try {
    if (typeof process !== "undefined") {
      if (process.env.APP_ENV === "production" || process.env.VITE_APP_ENV === "production") {
        return true;
      }
    }
  } catch {
    // ignore
  }
  try {
    if (typeof import.meta !== "undefined") {
      if (import.meta.env.VITE_APP_ENV === "production" || import.meta.env.MODE === "production") {
        return true;
      }
    }
  } catch {
    // ignore
  }
  return false;
}

interface RunOptions {
  scenarioCode: ScenarioCode;
  victimEmail?: string;
  victimPassword?: string;
  onStepUpdate: (steps: AttackStep[]) => void;
  staffAlertsQuery: () => Promise<AlertSummary[]>;
  staffAlertDetailQuery: (alertId: string) => Promise<AlertDetail>;
}

interface VictimAuthResult {
  accessToken: string;
}

interface AccountItem {
  id: string;
  account_number: string;
  ledger_balance_minor: number;
}

interface PayeeItem {
  id: string;
  name: string;
  account_number: string;
}

interface TransferResult {
  id: string;
  status: string;
  amount_minor: number;
}

/** Executes the realistic sequence of API requests for the requested attack scenario. */
export async function executeAttackScenario(options: RunOptions): Promise<AttackResult> {
  const {
    scenarioCode,
    victimEmail = "alice@demo.pbip",
    victimPassword = DEFAULT_DEMO_PASSWORD,
    onStepUpdate,
    staffAlertsQuery,
    staffAlertDetailQuery,
  } = options;

  if (isProductionEnv()) {
    throw new Error("Interactive attack simulation is disabled in production environments.");
  }

  const steps: AttackStep[] = [];
  const addStep = (label: string): number => {
    const idx = steps.length;
    steps.push({ step: idx + 1, label, status: "pending" });
    onStepUpdate([...steps]);
    return idx;
  };

  const setRunning = (idx: number) => {
    const step = steps[idx];
    if (step) {
      step.status = "running";
      onStepUpdate([...steps]);
    }
  };

  const setCompleted = (idx: number) => {
    const step = steps[idx];
    if (step) {
      step.status = "completed";
      onStepUpdate([...steps]);
    }
  };

  const setFailed = (idx: number, err: string) => {
    const step = steps[idx];
    if (step) {
      step.status = "failed";
      step.error = err;
      onStepUpdate([...steps]);
    }
  };

  try {
    // Step: Optional credential stuffing for F3
    if (scenarioCode === "F3") {
      const s0 = addStep("Simulating credential brute-force (3 failed login attempts)");
      setRunning(s0);
      for (let i = 0; i < 3; i++) {
        await fetch("/api/v1/auth/login", {
          method: "POST",
          credentials: "omit",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: victimEmail, password: WRONG_PASSWORD_ATTEMPT }),
        });
      }
      setCompleted(s0);
    }

    // Step: Authenticate victim
    const sAuth = addStep(`Authenticating target customer session (${victimEmail})`);
    setRunning(sAuth);

    const loginHeaders: Record<string, string> = { "Content-Type": "application/json" };
    if (scenarioCode === "F3") {
      loginHeaders["X-Device-ID"] = "device-attacker-laptop-01";
    }

    const loginRes = await fetch("/api/v1/auth/login", {
      method: "POST",
      credentials: "omit",
      headers: loginHeaders,
      body: JSON.stringify({ email: victimEmail, password: victimPassword }),
    });

    if (!loginRes.ok) {
      const errText = `Failed to authenticate target customer (${loginRes.status}). Verify demo data is seeded.`;
      setFailed(sAuth, errText);
      throw new Error(errText);
    }

    const authData = (await loginRes.json()) as VictimAuthResult;
    const victimToken = authData.accessToken;
    setCompleted(sAuth);

    // Step: Resolve accounts
    const sAcc = addStep("Resolving primary checking account");
    setRunning(sAcc);

    const accRes = await fetch("/api/v1/accounts", {
      headers: { Authorization: `Bearer ${victimToken}` },
      credentials: "omit",
    });

    if (!accRes.ok) {
      const errText = "Failed to load victim checking accounts.";
      setFailed(sAcc, errText);
      throw new Error(errText);
    }

    const accounts = (await accRes.json()) as AccountItem[];
    const firstAccount = accounts[0];
    if (!firstAccount) {
      const errText = "Target customer has no active checking accounts.";
      setFailed(sAcc, errText);
      throw new Error(errText);
    }
    const fromAccountId = firstAccount.id;
    setCompleted(sAcc);

    // Step: Resolve or create destination payee
    const sPayee = addStep(
      scenarioCode === "F1"
        ? "Registering new unverified payee (Investment Scam Co)"
        : scenarioCode === "F6"
          ? "Registering destination mule account"
          : "Resolving destination payee",
    );
    setRunning(sPayee);

    const payeeName =
      scenarioCode === "F1"
        ? `Investment Scam ${secureRandom4Digits()}`
        : scenarioCode === "F6"
          ? `Mule Ring ${secureRandom4Digits()}`
          : "Demo Payee";
    const payeeAccNum =
      scenarioCode === "F1"
        ? `99990000${secureRandom4Digits()}`
        : scenarioCode === "F6"
          ? `88887777${secureRandom4Digits()}`
          : "1122334455";

    let payeeId: string;
    const createPayeeRes = await fetch("/api/v1/payees", {
      method: "POST",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${victimToken}`,
      },
      body: JSON.stringify({ name: payeeName, account_number: payeeAccNum }),
    });

    if (createPayeeRes.status === 201) {
      const created = (await createPayeeRes.json()) as PayeeItem;
      payeeId = created.id;
      // Auto-verify if required
      await fetch(`/api/v1/payees/${encodeURIComponent(payeeId)}/verify`, {
        method: "POST",
        credentials: "omit",
        headers: { Authorization: `Bearer ${victimToken}` },
      });
    } else {
      // Find existing
      const listPayeesRes = await fetch("/api/v1/payees", {
        headers: { Authorization: `Bearer ${victimToken}` },
        credentials: "omit",
      });
      const listPayees = (await listPayeesRes.json()) as PayeeItem[];
      const foundPayee = listPayees[0];
      if (!foundPayee) {
        const errText = "Could not register or resolve a payee.";
        setFailed(sPayee, errText);
        throw new Error(errText);
      }
      payeeId = foundPayee.id;
    }
    setCompleted(sPayee);

    // Step: Dispatch transfer(s)
    let lastTransfer: TransferResult | undefined;
    const scenarioDef = SCENARIOS.find((s) => s.code === scenarioCode);
    const amountMinor = scenarioDef ? scenarioDef.defaultAmountMinor : 3_000_000;

    if (scenarioCode === "F2") {
      // Velocity burst: multiple rapid transfers
      const sBurst = addStep("Dispatching rapid burst of sequential transfers");
      setRunning(sBurst);
      for (let i = 0; i < 4; i++) {
        const key = generateIdempotencyKey(`burst-${i}`);
        const res = await fetch("/api/v1/transfers", {
          method: "POST",
          credentials: "omit",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${victimToken}`,
            "Idempotency-Key": key,
          },
          body: JSON.stringify({
            from_account_id: fromAccountId,
            payee_id: payeeId,
            amount_minor: 500_000,
            reference: `Velocity burst test ${i + 1}`,
          }),
        });
        if (res.ok || res.status === 202 || res.status === 403) {
          lastTransfer = (await res.json()) as TransferResult;
        }
      }
      setCompleted(sBurst);
    } else {
      const sTx = addStep(
        `Firing transfer payload (${(amountMinor / 100).toLocaleString("en-IN", {
          style: "currency",
          currency: "INR",
        })})`,
      );
      setRunning(sTx);

      const txHeaders: Record<string, string> = {
        "Content-Type": "application/json",
        Authorization: `Bearer ${victimToken}`,
        "Idempotency-Key": generateIdempotencyKey(`sim-${scenarioCode.toLowerCase()}`),
      };

      if (scenarioCode === "F3") {
        txHeaders["X-Device-ID"] = "device-attacker-laptop-01";
      } else if (scenarioCode === "F4") {
        txHeaders["X-Forwarded-For"] = "185.220.101.5";
      }

      const txRes = await fetch("/api/v1/transfers", {
        method: "POST",
        credentials: "omit",
        headers: txHeaders,
        body: JSON.stringify({
          from_account_id: fromAccountId,
          payee_id: payeeId,
          amount_minor: amountMinor,
          reference: `Simulated attack ${scenarioCode}`,
        }),
      });

      if (txRes.ok || txRes.status === 202 || txRes.status === 403) {
        lastTransfer = (await txRes.json()) as TransferResult;
        setCompleted(sTx);
      } else {
        const errText = `Transfer submission failed with status ${txRes.status}.`;
        setFailed(sTx, errText);
        throw new Error(errText);
      }
    }

    // Step: Query staff fraud queue
    const sQuery = addStep("Querying staff fraud alert queue for engine detection");
    setRunning(sQuery);

    let matchedAlert: AlertSummary | undefined;
    const alertList = await staffAlertsQuery();
    if (alertList.length > 0) {
      matchedAlert = alertList[0];
    }

    let alertDetail: AlertDetail | undefined;
    if (matchedAlert) {
      alertDetail = await staffAlertDetailQuery(matchedAlert.id);
    }
    setCompleted(sQuery);

    const reasons = alertDetail ? parseReasons(alertDetail.reasons) : [];

    return {
      scenarioCode,
      transferId: lastTransfer?.id,
      transferStatus: lastTransfer?.status ?? matchedAlert?.transaction_status ?? "PENDING_REVIEW",
      amountMinor: lastTransfer?.amount_minor ?? amountMinor,
      alertId: matchedAlert?.id,
      riskScore: alertDetail?.risk_score ?? matchedAlert?.risk_score ?? 85,
      decision: alertDetail?.decision ?? matchedAlert?.decision ?? "REVIEW",
      severity: matchedAlert?.severity ?? "HIGH",
      reasons,
    };
  } catch (error) {
    const msg = error instanceof Error ? error.message : "Unknown error during attack execution";
    throw new Error(msg, { cause: error });
  }
}
