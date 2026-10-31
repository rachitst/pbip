import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuditLogOut, ChainStatusOut, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { AUDIT_PAGE_SIZE, AuditLog, detailValue, detailsSummary } from "./AuditLog";

const complianceOfficerMe: UserOut = {
  id: "user-co",
  email: "compliance@demo.pbip",
  roles: ["COMPLIANCE_OFFICER"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const adminMe: UserOut = {
  id: "user-admin",
  email: "admin@demo.pbip",
  roles: ["ADMIN"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const nonComplianceStaffMe: UserOut = {
  id: "user-fa",
  email: "analyst@demo.pbip",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const mockLogs: AuditLogOut[] = [
  {
    seq: 102,
    actor_user_id: "00000000-0000-0000-0000-000000000001",
    action: "USER_LOCKED",
    resource_type: "user",
    resource_id: "00000000-0000-0000-0000-000000000002",
    request_id: "req-102",
    ip: "192.168.1.10",
    details: { reason: "excessive_logins" },
    created_at: "2026-02-01T12:00:00Z",
    hash: "a".repeat(64),
  },
  {
    seq: 101,
    actor_user_id: null,
    action: "SYSTEM_BOOT",
    resource_type: "system",
    resource_id: null,
    request_id: null,
    ip: "10.0.0.1",
    details: { version: "1.0.0" },
    created_at: "2026-02-01T10:00:00Z",
    hash: "b".repeat(64),
  },
  {
    seq: 100,
    actor_user_id: "00000000-0000-0000-0000-000000000003",
    action: "GENESIS",
    resource_type: "system",
    resource_id: null,
    request_id: null,
    ip: null,
    details: {},
    created_at: "2026-02-01T08:00:00Z",
    hash: "c".repeat(64),
  },
];

function renderWithClient(ui: React.ReactElement, user: UserOut = complianceOfficerMe) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MeContext.Provider value={user}>{ui}</MeContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Compliance Audit Log UI (TASK-048)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("1. Loading state renders cleanly", () => {
    fetchMock.mockImplementation(() => new Promise(() => {}));
    renderWithClient(<AuditLog />);
    expect(screen.getByText("Loading audit logs...")).toBeInTheDocument();
  });

  it("2. Handles empty state when no audit log entries exist", async () => {
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, []));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    renderWithClient(<AuditLog />);
    expect(await screen.findByText("No audit log entries recorded yet.")).toBeInTheDocument();
  });

  it("3. Displays chronological audit log entries with user, action, IP address, and timestamp", async () => {
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, mockLogs));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    renderWithClient(<AuditLog />);
    const table = await screen.findByRole("table");
    expect(table).toBeInTheDocument();

    expect(within(table).getByText("#102")).toBeInTheDocument();
    expect(within(table).getByText("USER_LOCKED")).toBeInTheDocument();
    expect(within(table).getByText("192.168.1.10")).toBeInTheDocument();

    expect(within(table).getByText("#101")).toBeInTheDocument();
    expect(within(table).getByText("SYSTEM_BOOT")).toBeInTheDocument();
    expect(within(table).getByText("10.0.0.1")).toBeInTheDocument();
    expect(within(table).getByText("System")).toBeInTheDocument();

    expect(within(table).getByText("#100")).toBeInTheDocument();
    expect(within(table).getByText("GENESIS")).toBeInTheDocument();
    const row100 = table.querySelector('tr[data-seq="100"]');
    expect(row100).not.toBeNull();
    if (row100) {
      expect(
        within(row100 as HTMLElement).getByTitle("No IP address recorded for this event"),
      ).toBeInTheDocument();
    }
  });

  it("4. Verifies hash chain successfully when intact", async () => {
    const validResult: ChainStatusOut = {
      valid: true,
      checked: 102,
      first_invalid_seq: null,
    };

    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, mockLogs));
      }
      if (url.pathname === "/api/v1/compliance/audit-logs/verify") {
        return Promise.resolve(json(200, validResult));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await screen.findByRole("table");

    const verifyBtn = screen.getByRole("button", { name: "Verify hash chain" });
    await user.click(verifyBtn);

    expect(
      await screen.findByText(
        /Hash chain intact\. Verified 102 audit log entries with no tampering detected\./,
      ),
    ).toBeInTheDocument();
  });

  it("5. Visually highlights data tampering and broken chain when verification fails", async () => {
    const brokenResult: ChainStatusOut = {
      valid: false,
      checked: 50,
      first_invalid_seq: 102,
    };

    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, mockLogs));
      }
      if (url.pathname === "/api/v1/compliance/audit-logs/verify") {
        return Promise.resolve(json(200, brokenResult));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await screen.findByRole("table");

    const verifyBtn = screen.getByRole("button", { name: "Verify hash chain" });
    await user.click(verifyBtn);

    expect(
      await screen.findByText(/Hash chain broken at entry #102\. Entries #1–#101 verified\./),
    ).toBeInTheDocument();

    expect(screen.getByText("TAMPERED")).toBeInTheDocument();
  });

  it("6. Handles verification request failure and provides a working retry button", async () => {
    let failVerify = true;
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, mockLogs));
      }
      if (url.pathname === "/api/v1/compliance/audit-logs/verify") {
        if (failVerify) {
          return Promise.resolve(
            json(500, { code: "internal_error", title: "Service unavailable" }),
          );
        }
        return Promise.resolve(json(200, { valid: true, checked: 3, first_invalid_seq: null }));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Verify hash chain" }));

    expect(await screen.findByText(/Verification request failed/)).toBeInTheDocument();

    failVerify = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(/Hash chain intact/)).toBeInTheDocument();
  });

  it("7. Supports keyboard accessibility on the Verify Hash Chain button", async () => {
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        return Promise.resolve(json(200, mockLogs));
      }
      if (url.pathname === "/api/v1/compliance/audit-logs/verify") {
        return Promise.resolve(json(200, { valid: true, checked: 102, first_invalid_seq: null }));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await screen.findByRole("table");

    const verifyBtn = screen.getByRole("button", { name: "Verify hash chain" });
    verifyBtn.focus();
    expect(verifyBtn).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(await screen.findByText(/Hash chain intact/)).toBeInTheDocument();
  });

  it("8. Allows ADMIN role and enforces access restriction for unauthorized staff roles", () => {
    const { unmount } = renderWithClient(<AuditLog />, adminMe);
    expect(screen.getByRole("heading", { name: "Compliance audit log" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify hash chain" })).toBeInTheDocument();
    unmount();

    renderWithClient(<AuditLog />, nonComplianceStaffMe);
    expect(
      screen.getByText(
        /Access restricted: The compliance audit log requires the Compliance Officer or Administrator role\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verify hash chain" })).not.toBeInTheDocument();
  });
});

/** `count` entries, newest first, ending at `oldest`. */
function entries(newest: number, count: number): AuditLogOut[] {
  return Array.from({ length: count }, (_, i) => ({
    ...mockLogs[0],
    seq: newest - i,
    request_id: null,
    hash: "d".repeat(64),
  })) as AuditLogOut[];
}

describe("TASK-048 review fixes", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function serveLog(total: number, verify?: ChainStatusOut) {
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") {
        const before = Number(url.searchParams.get("before_seq") ?? total + 1);
        const limit = Number(url.searchParams.get("limit"));
        const newest = before - 1;
        return Promise.resolve(json(200, entries(newest, Math.max(0, Math.min(limit, newest)))));
      }
      if (url.pathname === "/api/v1/compliance/audit-logs/verify" && verify) {
        return Promise.resolve(json(200, verify));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  }

  it("pages through older entries with before_seq until the oldest page", async () => {
    serveLog(120);
    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    const table = await screen.findByRole("region", { name: "Compliance audit logs table" });
    expect(within(table).getAllByRole("row")).toHaveLength(AUDIT_PAGE_SIZE + 1); // + header

    await user.click(screen.getByRole("button", { name: "Load older entries" }));
    expect(await within(table).findByText("#70")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => urlOf(input).includes("before_seq=71"))).toBe(
      true,
    );

    await user.click(screen.getByRole("button", { name: "Load older entries" }));
    expect(await within(table).findByText("#1")).toBeInTheDocument();
    expect(within(table).getAllByRole("row")).toHaveLength(120 + 1);
    expect(screen.queryByRole("button", { name: "Load older entries" })).not.toBeInTheDocument();
  });

  it("says when the broken entry is older than the loaded page, and marks it once loaded", async () => {
    serveLog(120, { valid: false, checked: 120, first_invalid_seq: 5 });
    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await screen.findByRole("region", { name: "Compliance audit logs table" });

    await user.click(screen.getByRole("button", { name: "Verify hash chain" }));
    expect(await screen.findByText(/Entry #5 is older than the entries shown/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load older entries" }));
    await user.click(await screen.findByRole("button", { name: "Load older entries" }));
    const row = (await screen.findByText("#5")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("TAMPERED")).toBeInTheDocument();
    expect(screen.queryByText(/Entry #5 is older than the entries shown/)).not.toBeInTheDocument();
  });

  it("shows the server's reason when the log cannot be loaded", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ code: "forbidden", title: "Multi-factor authentication required" }),
        { status: 403, headers: { "Content-Type": "application/problem+json" } },
      ),
    );
    renderWithClient(<AuditLog />);
    expect(await screen.findByText(/Multi-factor authentication required/)).toBeInTheDocument();
  });
});

describe("TASK-048 visual review changes", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function serve(logs: AuditLogOut[], verify?: ChainStatusOut) {
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname === "/api/v1/compliance/audit-logs") return Promise.resolve(json(200, logs));
      if (url.pathname === "/api/v1/compliance/audit-logs/verify" && verify) {
        return Promise.resolve(json(200, verify));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  }

  it("summarises details with their actual values", () => {
    expect(detailValue(["CUSTOMER", "ADMIN"])).toBe("CUSTOMER, ADMIN");
    expect(detailValue({ a: 1 })).toBe('{"a":1}');
    expect(detailValue(null)).toBe("—");
    expect(detailsSummary({ roles: ["CUSTOMER"] })).toBe("roles: CUSTOMER");
    expect(detailsSummary({ status: "POSTED", amount_minor: 64900, ip: "1.2.3.4", risk: 45 })).toBe(
      "status: POSTED · amount_minor: 64900 · +1 more",
    );
  });

  it("expands a row to list every detail field, and collapses it again", async () => {
    const entry = {
      ...mockLogs[0],
      details: { roles: ["CUSTOMER"], status: "PENDING_REVIEW", risk_score: 45 },
    } as AuditLogOut;
    serve([entry]);
    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    const toggle = await screen.findByRole("button", { name: /details of entry #102/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("roles: CUSTOMER · status: PENDING_REVIEW · +1 more");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    const list = within(panel as HTMLElement);
    expect(list.getByText("risk_score")).toBeInTheDocument();
    expect(list.getByText("45")).toBeInTheDocument();
    expect(list.getByText("CUSTOMER")).toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(toggle.getAttribute("aria-controls") ?? "")).toBeNull();
  });

  it("words a break at the first entry without an empty range", async () => {
    serve(mockLogs, { valid: false, checked: 0, first_invalid_seq: 1 });
    const user = userEvent.setup();
    renderWithClient(<AuditLog />);
    await user.click(await screen.findByRole("button", { name: "Verify hash chain" }));
    expect(
      await screen.findByText(/Hash chain broken at entry #1\. No earlier entries to verify\./),
    ).toBeInTheDocument();
  });

  it("renders without inline styles (all styling comes from the stylesheet)", async () => {
    serve(mockLogs, { valid: false, checked: 101, first_invalid_seq: 102 });
    const user = userEvent.setup();
    const { container } = renderWithClient(<AuditLog />);
    await user.click(await screen.findByRole("button", { name: "Verify hash chain" }));
    await screen.findByText("TAMPERED");
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });
});
