import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertDetail, TransferOut, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { FraudDemoLab } from "./FraudDemoLab";

const analyst: UserOut = {
  id: "u-fa",
  email: "analyst@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const aliceAccount = {
  id: "acc-alice",
  account_number: "409443369528",
  account_number_masked: "••••••••9528",
  account_type: "SAVINGS",
  currency: "INR",
  status: "ACTIVE",
  ledger_balance_minor: 25_000_000,
  held_minor: 0,
  available_minor: 25_000_000,
};
const bobAccount = { ...aliceAccount, id: "acc-bob", account_number: "409443361111" };

function payee(id: string, name: string, bank = "EXTB0001", status = "VERIFIED") {
  return {
    id,
    name,
    account_number_masked: "••••1234",
    bank_code: bank,
    nickname: null,
    status,
    created_at: "2026-11-01T10:00:00Z",
    verified_at: status === "VERIFIED" ? "2026-11-01T10:00:00Z" : null,
  };
}

function transferOut(id: string, status: TransferOut["status"], amount: number): TransferOut {
  return {
    id,
    status,
    amount_minor: amount,
    currency: "INR",
    from_account_id: "acc-alice",
    to_account_id: null,
    payee_id: "p-1",
    reference: null,
    created_at: "2026-11-01T10:00:00Z",
    posted_at: status === "POSTED" ? "2026-11-01T10:00:00Z" : null,
    step_up_required: false,
    message: status === "POSTED" ? "Transfer completed" : "Transfer is under review",
  };
}

const alert: AlertDetail = {
  id: "al-77",
  status: "OPEN",
  severity: "MEDIUM",
  risk_score: 77,
  decision: "REVIEW",
  amount_minor: 3_000_000,
  currency: "INR",
  transaction_status: "PENDING_REVIEW",
  created_at: "2026-11-01T10:00:00Z",
  transaction_id: "tx-1",
  customer_id: "cu-1",
  customer: null,
  transaction_type: "TRANSFER",
  reference: "Lab F1 APP scam",
  reasons: [
    {
      code: "NEW_PAYEE_LARGE_AMOUNT",
      message: "Payee was added 0 minutes ago and INR 30,000.00 is a large amount",
      source: "RULE",
      contribution: 45,
    },
  ],
  features: {},
  model_version: "baseline-rules-0.3",
  latency_ms: 3.1,
  resolution_note: null,
  resolved_at: null,
};

interface Call {
  method: string;
  url: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
  body: unknown;
}

/** A scripted server: each handler sees the call and returns a Response (or undefined = 404). */
type Handler = (call: Call) => Response | undefined;

let calls: Call[] = [];
let handler: Handler = () => undefined;
let labEnabled = true;

function server(call: Call): Response {
  if (call.url.endsWith("/api/v1/fraud/demo-lab")) return json(200, { enabled: labEnabled });
  return handler(call) ?? json(404, { code: "not_found", title: "Not found" });
}

function customerCalls(): Call[] {
  return calls.filter((c) => !c.url.includes("/fraud/"));
}

function renderLab(me: UserOut = analyst) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>
          <FraudDemoLab />
        </MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

async function enterPassword() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Customer password"), "demo-pass");
  return user;
}

/** Login answers per email; everything else in a happy F1 world. */
function happyServer(overrides: Handler = () => undefined): Handler {
  return (call) => {
    const custom = overrides(call);
    if (custom) return custom;
    const { method, url } = call;
    if (url.endsWith("/auth/login")) {
      const { email, password } = call.body as { email: string; password: string };
      if (password !== "demo-pass") return json(401, { code: "unauthorized", title: "Invalid" });
      return json(200, { access_token: `token-${email}`, mfa_required: false });
    }
    const bearer = call.headers.get("Authorization");
    if (url.endsWith("/api/v1/accounts")) {
      return json(200, [bearer === "Bearer token-bob@demo.pbip" ? bobAccount : aliceAccount]);
    }
    if (url.endsWith("/api/v1/payees") && method === "GET") return json(200, []);
    if (url.endsWith("/api/v1/payees") && method === "POST") {
      const body = call.body as { name: string; bank_code: string };
      return json(201, payee("p-1", body.name, body.bank_code, "PENDING"));
    }
    if (url.endsWith("/verify")) {
      return json(200, { payee: payee("p-1", "Urgent Investments"), name_match: "NOT_CHECKED" });
    }
    if (url.endsWith("/api/v1/customers/devices")) return json(201, {});
    if (url.endsWith("/api/v1/transfers")) {
      return json(202, transferOut("tx-1", "PENDING_REVIEW", 3_000_000));
    }
    if (url.includes("/api/v1/fraud/alerts?")) {
      return json(200, url.includes("transaction_id=tx-1") ? [alert] : []);
    }
    if (url.endsWith("/api/v1/fraud/alerts/al-77")) return json(200, alert);
    return undefined;
  };
}

beforeEach(() => {
  calls = [];
  labEnabled = true;
  handler = happyServer();
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const raw = init?.body;
    const call: Call = {
      method: init?.method ?? "GET",
      url: urlOf(input),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
    };
    calls.push(call);
    return Promise.resolve(server(call));
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fraud demo lab (TASK-081)", () => {
  it("shows a card per scenario; F4 explains why it cannot run; runs wait for a password", async () => {
    renderLab();
    await screen.findByLabelText("Customer password");
    const cards = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(cards).toEqual([
      "F1 APP scam",
      "F2 Velocity burst",
      "F3 Account takeover",
      "F4 Impossible travel",
      "F5 Amount anomaly",
      "F6 Mule pass-through",
    ]);
    expect(screen.getAllByText("Run scenario")).toHaveLength(5); // every card but F4
    expect(screen.getByText(/does not forge X-Forwarded-For/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run F1 APP scam" })).toBeDisabled();
    await enterPassword();
    expect(screen.getByRole("button", { name: "Run F1 APP scam" })).toBeEnabled();
    expect(customerCalls()).toHaveLength(0);
  });

  it("F1 dispatches the real API sequence and shows the server's decision and reasons", async () => {
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F1 APP scam" }));

    expect(await screen.findByText("Risk 77/100")).toBeInTheDocument();
    expect(screen.getByText("Held for review")).toBeInTheDocument();
    expect(screen.getByText(/Payee was added 0 minutes ago/)).toBeInTheDocument();
    expect(screen.getByText("Transfer is under review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the alert" })).toHaveAttribute(
      "href",
      "/staff/fraud/al-77",
    );

    expect(calls.map((c) => `${c.method} ${new URL(c.url, "http://x").pathname}`)).toEqual([
      "GET /api/v1/fraud/demo-lab",
      "POST /api/v1/auth/login",
      "GET /api/v1/accounts",
      "POST /api/v1/payees",
      "POST /api/v1/payees/p-1/verify",
      "POST /api/v1/transfers",
      "GET /api/v1/fraud/alerts",
      "GET /api/v1/fraud/alerts/al-77",
    ]);
    // Customer calls carry the customer's token and never the staff cookie.
    for (const c of customerCalls().slice(1)) {
      expect(c.credentials).toBe("omit");
      expect(c.headers.get("Authorization")).toBe("Bearer token-alice@demo.pbip");
    }
    // The analyst reads the alert for THIS transfer, with the staff session.
    const lookup = calls.find((c) => c.url.includes("/fraud/alerts?"));
    expect(lookup?.url).toContain("transaction_id=tx-1");
    expect(lookup?.credentials).toBe("include");
    expect(lookup?.headers.get("Authorization")).not.toBe("Bearer token-alice@demo.pbip");
    const sent = calls.find((c) => c.url.endsWith("/api/v1/transfers"));
    expect(sent?.body).toMatchObject({ amount_minor: 3_000_000, from_account_id: "acc-alice" });
    expect(sent?.headers.get("Idempotency-Key")).toMatch(/^lab-[0-9a-f-]{36}$/);
  });

  it("shows no score when the engine raised no alert (nothing is made up)", async () => {
    handler = happyServer((call) =>
      call.url.endsWith("/api/v1/transfers")
        ? json(201, transferOut("tx-ok", "POSTED", 3_000_000))
        : undefined,
    );
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F1 APP scam" }));
    expect(await screen.findByText(/No alert: the engine did not hold/)).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.queryByText(/Risk \d+\/100/)).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/fraud/alerts/"))).toBe(false);
  });

  it("F2 sends four transfers, each with its own idempotency key", async () => {
    handler = happyServer((call) => {
      if (call.url.endsWith("/api/v1/payees") && call.method === "GET") {
        return json(200, [
          { ...payee("p-lab", "PBIP Lab Merchant"), account_number_masked: "•••••0081" },
        ]);
      }
      return undefined;
    });
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F2 Velocity burst" }));
    expect(
      await screen.findByRole("heading", { name: /F2 Velocity burst: finished/ }),
    ).toBeVisible();
    const transfers = calls.filter((c) => c.url.endsWith("/api/v1/transfers"));
    expect(transfers).toHaveLength(4);
    expect(new Set(transfers.map((t) => t.headers.get("Idempotency-Key"))).size).toBe(4);
    expect(transfers.every((t) => (t.body as { payee_id: string }).payee_id === "p-lab")).toBe(
      true,
    );
    expect(calls.some((c) => c.url.endsWith("/api/v1/payees") && c.method === "POST")).toBe(false);
  });

  it("F3 fails three sign-ins, then pays from a device that differs from the usual one", async () => {
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F3 Account takeover" }));
    expect(await screen.findByText("Risk 77/100")).toBeInTheDocument();
    const logins = calls.filter((c) => c.url.endsWith("/auth/login"));
    expect(logins.map((c) => (c.body as { password: string }).password === "demo-pass")).toEqual([
      true,
      false,
      false,
      false,
      true,
    ]);
    const usual = calls
      .find((c) => c.url.endsWith("/customers/devices"))
      ?.headers.get("X-Device-ID");
    const used = calls.find((c) => c.url.endsWith("/api/v1/transfers"))?.headers.get("X-Device-ID");
    expect(usual).toBeTruthy();
    expect(used).toMatch(/^lab-attacker-/);
    expect(used).not.toBe(usual);
  });

  it("F3 stops at a lockout instead of carrying on", async () => {
    let failures = 0;
    handler = happyServer((call) => {
      if (!call.url.endsWith("/auth/login")) return undefined;
      if ((call.body as { password: string }).password === "demo-pass") return undefined;
      failures += 1;
      return failures === 2
        ? json(423, { code: "account_locked", title: "Account locked" })
        : json(401, { code: "unauthorized", title: "Invalid" });
    });
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F3 Account takeover" }));
    expect(await screen.findByRole("heading", { name: /stopped/ })).toBeInTheDocument();
    expect(screen.getAllByText(/temporarily locked/).length).toBeGreaterThan(0);
    expect(calls.some((c) => c.url.endsWith("/api/v1/transfers"))).toBe(false);
  });

  it("F6 pays the mule from a second customer and forwards 95% only if it arrived", async () => {
    let transfers = 0;
    handler = happyServer((call) => {
      if (call.url.endsWith("/api/v1/customers/me")) return json(200, { full_name: "Alice Demo" });
      if (call.url.endsWith("/verify")) {
        return json(200, { payee: payee("p-1", "x", "PBIP0001"), name_match: "MATCH" });
      }
      if (call.url.endsWith("/api/v1/transfers")) {
        transfers += 1;
        return transfers === 1
          ? json(201, transferOut("tx-in", "POSTED", 2_400_000))
          : json(202, transferOut("tx-1", "PENDING_REVIEW", 2_280_000));
      }
      return undefined;
    });
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F6 Mule pass-through" }));
    expect(await screen.findByText("Risk 77/100")).toBeInTheDocument();
    const [inbound, forward] = calls.filter((c) => c.url.endsWith("/api/v1/transfers"));
    expect(inbound?.headers.get("Authorization")).toBe("Bearer token-bob@demo.pbip");
    expect(inbound?.body).toMatchObject({ amount_minor: 2_400_000, from_account_id: "acc-bob" });
    expect(forward?.headers.get("Authorization")).toBe("Bearer token-alice@demo.pbip");
    expect(forward?.body).toMatchObject({ amount_minor: 2_280_000 });
    const internal = calls.find(
      (c) =>
        c.method === "POST" &&
        c.url.endsWith("/api/v1/payees") &&
        c.headers.get("Authorization") === "Bearer token-bob@demo.pbip",
    );
    expect(internal?.body).toMatchObject({
      name: "Alice Demo",
      account_number: "409443369528",
      bank_code: "PBIP0001",
    });
  });

  it("F6 does not forward when the inbound payment was held", async () => {
    handler = happyServer((call) => {
      if (call.url.endsWith("/api/v1/customers/me")) return json(200, { full_name: "Alice Demo" });
      return undefined;
    });
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F6 Mule pass-through" }));
    expect(
      await screen.findByRole("heading", { name: /F6 Mule pass-through: finished/ }),
    ).toBeVisible();
    expect(calls.filter((c) => c.url.endsWith("/api/v1/transfers"))).toHaveLength(1);
  });

  it("a failed sign-in stops the run and says why; no money moves", async () => {
    handler = happyServer((call) =>
      call.url.endsWith("/auth/login")
        ? json(401, { code: "unauthorized", title: "Invalid email or password" })
        : undefined,
    );
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F1 APP scam" }));
    expect(
      await screen.findByRole("heading", { name: /F1 APP scam: stopped/ }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Invalid email or password").length).toBeGreaterThan(0);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/api/v1/transfers"))).toBe(false);
  });

  it("refuses a customer with an authenticator instead of guessing a code", async () => {
    handler = happyServer((call) =>
      call.url.endsWith("/auth/login")
        ? json(200, { mfa_required: true, mfa_token: "mfa" })
        : undefined,
    );
    renderLab();
    const user = await enterPassword();
    await user.click(screen.getByRole("button", { name: "Run F1 APP scam" }));
    expect((await screen.findAllByText(/has an authenticator enrolled/)).length).toBeGreaterThan(0);
    expect(calls.some((c) => c.url.endsWith("/api/v1/accounts"))).toBe(false);
  });

  it("is switched off when the server is in production: no forms, no runs", async () => {
    labEnabled = false;
    renderLab();
    expect(await screen.findByText(/switched off in this environment/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run / })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Customer password")).not.toBeInTheDocument();
    expect(customerCalls()).toHaveLength(0);
  });

  it("shows an error with retry when the lab status cannot be loaded", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(json(500, { title: "boom" }));
    renderLab();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not check whether the lab may run here.",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("is restricted to fraud analysts and admins and never asks the server", async () => {
    renderLab({ ...analyst, roles: ["COMPLIANCE_OFFICER"] });
    expect(await screen.findByText(/Access restricted/)).toBeInTheDocument();
    await waitFor(() => {
      expect(calls).toHaveLength(0);
    });
  });
});
