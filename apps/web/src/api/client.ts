import type { components } from "./schema";

export type TokenResponse = components["schemas"]["TokenResponse"];
export type AccountOut = components["schemas"]["AccountOut"];
export type PayeeOut = components["schemas"]["PayeeOut"];
export type TransferOut = components["schemas"]["TransferOut"];
export type PayeeVerificationOut = components["schemas"]["PayeeVerificationOut"];
export type PayeeUpdate = components["schemas"]["PayeeUpdate"];
export type BillerOut = components["schemas"]["BillerOut"];
export type BillPaymentOut = components["schemas"]["BillPaymentOut"];
export type UserOut = components["schemas"]["UserOut"];
export type MfaEnrollmentOut = components["schemas"]["MfaEnrollmentOut"];
export type RecoveryCodesOut = components["schemas"]["RecoveryCodesOut"];
export type CustomerProfileOut = components["schemas"]["CustomerProfileOut"];
export type DeviceOut = components["schemas"]["DeviceOut"];
export type EntriesPage = components["schemas"]["EntriesPage"];
export type EntryOut = components["schemas"]["EntryOut"];
export type OpenAccountType = components["schemas"]["OpenAccountType"];
export type NotificationOut = components["schemas"]["NotificationOut"];
export type NotificationsPage = components["schemas"]["NotificationsPage"];
export type BulkMarkReadResponse = components["schemas"]["BulkMarkReadResponse"];
export type AuditLogOut = components["schemas"]["AuditLogOut"];
export type ChainStatusOut = components["schemas"]["ChainStatusOut"];
export type AlertSummary = components["schemas"]["AlertSummary"];
export type AlertDetail = components["schemas"]["AlertDetail"];
export type FraudStats = components["schemas"]["FraudStats"];
export type SecurityMonitoring = components["schemas"]["SecurityMonitoring"];
export type DemoLabStatus = components["schemas"]["DemoLabStatus"];
export type SpendingBreakdownResponse = components["schemas"]["SpendingBreakdownResponse"];
export type CategorySpendingItem = components["schemas"]["CategorySpendingItem"];
export type SpendingTrendsResponse = components["schemas"]["SpendingTrendsResponse"];
export type MonthlySpendingSummary = components["schemas"]["MonthlySpendingSummary"];
export type CategoryShiftItem = components["schemas"]["CategoryShiftItem"];
export type FinancialHealthScoreResponse = components["schemas"]["FinancialHealthScoreResponse"];
export type FinancialHealthComponents = components["schemas"]["FinancialHealthComponents"];
export type HealthScoreComponent = components["schemas"]["HealthScoreComponent"];
export type ChatResponse = components["schemas"]["ChatResponse"];
export type AssistantHistory = components["schemas"]["AssistantHistory"];
export type AssistantMessageOut = components["schemas"]["AssistantMessageOut"];
export type StreamToolCall = components["schemas"]["StreamToolCall"];
export type StreamToolResult = components["schemas"]["StreamToolResult"];
export type StreamToken = components["schemas"]["StreamToken"];
export type StreamError = components["schemas"]["StreamError"];
export type LoanProductOut = components["schemas"]["LoanProductOut"];
export type EmiOut = components["schemas"]["EmiOut"];
export type EligibilityOut = components["schemas"]["EligibilityOut"];
export type LoanApplyRequest = components["schemas"]["LoanApplyRequest"];
export type LoanSummaryOut = components["schemas"]["LoanSummaryOut"];
export type LoanOut = components["schemas"]["LoanOut"];
export type LoanInstalmentOut = components["schemas"]["LoanInstalmentOut"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** Server request id from the problem body, for support; never contains customer data. */
    readonly requestId: string | null = null,
  ) {
    super(message);
  }
}

/** The access token lives ONLY in memory (never localStorage): XSS cannot exfiltrate it for
 * later use, and the HttpOnly refresh cookie restores the session after a page reload. */
let accessToken: string | null = null;
let onSessionLost: (() => void) | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}
export function setSessionLostHandler(handler: (() => void) | null): void {
  onSessionLost = handler;
}

interface Problem {
  code?: string;
  title?: string;
  request_id?: string | null;
}

async function toError(response: Response): Promise<ApiError> {
  const problem = (await response.json().catch(() => ({}))) as Problem;
  return new ApiError(
    response.status,
    problem.code ?? "error",
    problem.title ?? response.statusText,
    problem.request_id ?? null,
  );
}

function isProblem(response: Response): boolean {
  return (response.headers.get("Content-Type") ?? "").includes("application/problem+json");
}

async function refreshSession(): Promise<boolean> {
  const response = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    credentials: "include",
    headers: { "X-Requested-With": "pbip-web" },
  });
  if (!response.ok) return false;
  const body = (await response.json()) as TokenResponse;
  accessToken = body.access_token ?? null;
  return accessToken !== null;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, { ...init, headers, credentials: "include" });

  if (response.status === 401 && retry && accessToken !== null) {
    if (await refreshSession()) return apiFetch<T>(path, init, false);
    accessToken = null;
    onSessionLost?.();
  }
  return readBody<T>(path, response);
}

async function readBody<T>(path: string, response: Response): Promise<T> {
  // Transfer and bill-payment bodies for 202 (pending) and 403 (declined) carry state the UI must
  // show. They are plain JSON; a problem+json 403 (e.g. a non-customer token) is an error.
  const isTransferState =
    (path.startsWith("/api/v1/transfers") || path === "/api/v1/bills/pay") &&
    (response.status === 202 || response.status === 403) &&
    !isProblem(response);
  if (!response.ok && !isTransferState) throw await toError(response);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/** Authenticated GET of a file (statements): the bearer token never goes into a URL, so the
 * download is fetched as a blob and handed to the browser. Same refresh retry as apiFetch. */
export async function apiDownload(
  path: string,
  retry = true,
): Promise<{ blob: Blob; filename: string | null }> {
  const headers = new Headers();
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, { headers, credentials: "include" });
  if (response.status === 401 && retry && accessToken !== null) {
    if (await refreshSession()) return apiDownload(path, false);
    accessToken = null;
    onSessionLost?.();
  }
  if (!response.ok) throw await toError(response);
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? null;
  return { blob: await response.blob(), filename };
}

/** A call made as ANOTHER principal with an explicit bearer token (the staff fraud demo lab,
 * TASK-081, acting as a demo customer). Cookies are omitted both ways, so the signed-in staff
 * session (memory token + HttpOnly refresh cookie) is neither sent nor replaced. No refresh:
 * the token lives only for one scenario run. */
export async function apiFetchAs<T>(
  token: string | null,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers, credentials: "omit" });
  return readBody<T>(path, response);
}

/** POST that returns the raw Response for streaming (SSE). Same auth and one refresh retry as
 * apiFetch; a non-2xx answer is thrown as ApiError so callers handle 401/422/429 uniformly. */
export async function apiStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  retry = true,
): Promise<Response> {
  const headers = new Headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (response.status === 401 && retry && accessToken !== null) {
    if (await refreshSession()) return apiStream(path, body, signal, false);
    accessToken = null;
    onSessionLost?.();
  }
  if (!response.ok) throw await toError(response);
  return response;
}

export const api = {
  login: (email: string, password: string) =>
    apiFetch<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  verifyMfa: (mfaToken: string, code: string) =>
    apiFetch<TokenResponse>("/api/v1/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ mfa_token: mfaToken, code }),
    }),
  register: (fullName: string, email: string, password: string) =>
    apiFetch<UserOut>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ full_name: fullName, email, password }),
    }),
  enrollMfa: () => apiFetch<MfaEnrollmentOut>("/api/v1/auth/mfa/enroll", { method: "POST" }),
  confirmMfa: (code: string) =>
    apiFetch<RecoveryCodesOut>("/api/v1/auth/mfa/confirm", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  restoreSession: refreshSession,
  logout: () =>
    apiFetch<undefined>("/api/v1/auth/logout", {
      method: "POST",
      headers: { "X-Requested-With": "pbip-web" },
    }),
  me: () => apiFetch<UserOut>("/api/v1/auth/me"),
  accounts: () => apiFetch<AccountOut[]>("/api/v1/accounts"),
  openAccount: (accountType: OpenAccountType) =>
    apiFetch<AccountOut>("/api/v1/accounts", {
      method: "POST",
      body: JSON.stringify({ account_type: accountType }),
    }),
  /** Keyset pagination: pass the previous page's next_cursor to walk further back in time. */
  accountEntries: (accountId: string, cursor: string | null, limit = 10) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<EntriesPage>(
      `/api/v1/accounts/${encodeURIComponent(accountId)}/transactions?${params.toString()}`,
    );
  },
  profile: () => apiFetch<CustomerProfileOut>("/api/v1/customers/me"),
  devices: () => apiFetch<DeviceOut[]>("/api/v1/customers/devices"),
  registerDevice: (deviceId: string, label: string) =>
    apiFetch<DeviceOut>("/api/v1/customers/devices", {
      method: "POST",
      headers: { "X-Device-ID": deviceId },
      body: JSON.stringify({ label }),
    }),
  /** 204 on success; the server revokes every session, so the caller must sign out. */
  changePassword: (currentPassword: string, newPassword: string) =>
    apiFetch<undefined>("/api/v1/auth/password-change", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
  /** UC-03 / TASK-016: dates are YYYY-MM-DD (bank days, IST). */
  statement: (accountId: string, start: string, end: string, format: "pdf" | "csv") =>
    apiDownload(
      `/api/v1/accounts/${encodeURIComponent(accountId)}/statements?${new URLSearchParams({
        start_date: start,
        end_date: end,
        format,
      }).toString()}`,
    ),
  payees: () => apiFetch<PayeeOut[]>("/api/v1/payees"),
  addPayee: (body: {
    name: string;
    account_number: string;
    bank_code: string;
    nickname: string | null;
  }) => apiFetch<PayeeOut>("/api/v1/payees", { method: "POST", body: JSON.stringify(body) }),
  verifyPayee: (payeeId: string) =>
    apiFetch<PayeeVerificationOut>(`/api/v1/payees/${encodeURIComponent(payeeId)}/verify`, {
      method: "POST",
    }),
  updatePayee: (payeeId: string, body: PayeeUpdate) =>
    apiFetch<PayeeOut>(`/api/v1/payees/${encodeURIComponent(payeeId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  removePayee: (payeeId: string) =>
    apiFetch<undefined>(`/api/v1/payees/${encodeURIComponent(payeeId)}`, { method: "DELETE" }),
  billers: () => apiFetch<BillerOut[]>("/api/v1/bills/billers"),
  billPayments: () => apiFetch<BillPaymentOut[]>("/api/v1/bills/payments?limit=20"),
  /** Same one-key-per-intent rule as ``transfer``. 202 (held) and 403 (declined) are outcomes. */
  payBill: (
    body: {
      from_account_id: string;
      biller_id: string;
      consumer_reference: string;
      amount_minor: number;
    },
    idempotencyKey: string,
  ) =>
    apiFetch<BillPaymentOut>("/api/v1/bills/pay", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
  /** One Idempotency-Key per user intent; reuse the SAME key when retrying the same submit. */
  transfer: (
    body: {
      from_account_id: string;
      payee_id: string;
      amount_minor: number;
      reference?: string | null;
    },
    idempotencyKey: string,
  ) =>
    apiFetch<TransferOut>("/api/v1/transfers", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
  /** Complete a PENDING_STEP_UP transfer. 200 with POSTED, or PENDING_REVIEW after the third
   * wrong code (the server escalates); 422 for a wrong code; 409 if it is no longer waiting. */
  stepUp: (transferId: string, code: string) =>
    apiFetch<TransferOut>(`/api/v1/transfers/${encodeURIComponent(transferId)}/step-up`, {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  loanProducts: () => apiFetch<LoanProductOut[]>("/api/v1/loans/products"),
  /** The server computes every EMI (exact integer paise); the browser never does money maths. */
  calculateEmi: (productId: string, principalMinor: number, tenureMonths: number) =>
    apiFetch<EmiOut>("/api/v1/loans/calculate-emi", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        principal_minor: principalMinor,
        tenure_months: tenureMonths,
      }),
    }),
  checkEligibility: (productId: string, amountMinor: number, tenureMonths: number) =>
    apiFetch<EligibilityOut>("/api/v1/loans/check-eligibility", {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        amount_minor: amountMinor,
        tenure_months: tenureMonths,
      }),
    }),
  /** Same one-key-per-intent rule as ``transfer``: a retried submit never disburses twice. */
  applyForLoan: (body: LoanApplyRequest, idempotencyKey: string) =>
    apiFetch<LoanOut>("/api/v1/loans/apply", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
  loans: () => apiFetch<LoanSummaryOut[]>("/api/v1/loans"),
  loan: (loanId: string) => apiFetch<LoanOut>(`/api/v1/loans/${encodeURIComponent(loanId)}`),
  notifications: (options?: { unread_only?: boolean; limit?: number; offset?: number }) => {
    const params = new URLSearchParams();
    if (options?.unread_only !== undefined) params.set("unread_only", String(options.unread_only));
    if (options?.limit !== undefined) params.set("limit", String(options.limit));
    if (options?.offset !== undefined) params.set("offset", String(options.offset));
    const qs = params.toString();
    return apiFetch<NotificationsPage>(`/api/v1/notifications${qs ? `?${qs}` : ""}`);
  },
  markNotificationRead: (notificationId: string) =>
    apiFetch<undefined>(`/api/v1/notifications/${encodeURIComponent(notificationId)}/read`, {
      method: "POST",
    }),
  markAllNotificationsRead: () =>
    apiFetch<BulkMarkReadResponse>("/api/v1/notifications/mark-all-read", {
      method: "POST",
    }),
  auditLogs: (limit = 50, beforeSeq?: number) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (beforeSeq !== undefined) params.set("before_seq", String(beforeSeq));
    return apiFetch<AuditLogOut[]>(`/api/v1/compliance/audit-logs?${params.toString()}`);
  },
  verifyAuditChain: () => apiFetch<ChainStatusOut>("/api/v1/compliance/audit-logs/verify"),
  fraudAlerts: (status?: string, transactionId?: string) => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (transactionId) params.set("transaction_id", transactionId);
    return apiFetch<AlertSummary[]>(`/api/v1/fraud/alerts?${params.toString()}`);
  },
  fraudDemoLab: () => apiFetch<DemoLabStatus>("/api/v1/fraud/demo-lab"),
  fraudAlert: (alertId: string) =>
    apiFetch<AlertDetail>(`/api/v1/fraud/alerts/${encodeURIComponent(alertId)}`),
  decideAlert: (alertId: string, action: "RELEASE" | "REJECT", note: string) =>
    apiFetch<undefined>(`/api/v1/fraud/alerts/${encodeURIComponent(alertId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ action, note: note.trim() === "" ? null : note.trim() }),
    }),
  fraudStats: (days: number) => apiFetch<FraudStats>(`/api/v1/fraud/stats?days=${days}`),
  securityMonitoring: () => apiFetch<SecurityMonitoring>("/api/v1/fraud/monitoring"),
  spendingBreakdown: (accountId: string, year?: number, month?: number) => {
    const params = new URLSearchParams({ account_id: accountId });
    if (year !== undefined) params.set("year", String(year));
    if (month !== undefined) params.set("month", String(month));
    return apiFetch<SpendingBreakdownResponse>(`/api/v1/analytics/spending?${params.toString()}`);
  },
  spendingTrends: (accountId: string, year?: number, month?: number) => {
    const params = new URLSearchParams({ account_id: accountId });
    if (year !== undefined) params.set("year", String(year));
    if (month !== undefined) params.set("month", String(month));
    return apiFetch<SpendingTrendsResponse>(`/api/v1/analytics/trends?${params.toString()}`);
  },
  financialHealthScore: (accountId: string) => {
    const params = new URLSearchParams({ account_id: accountId });
    return apiFetch<FinancialHealthScoreResponse>(
      `/api/v1/analytics/health-score?${params.toString()}`,
    );
  },
  assistantHistory: (limit = 50) =>
    apiFetch<AssistantHistory>(`/api/v1/assistant/messages?limit=${String(limit)}`),
};
