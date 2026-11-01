import type { components } from "./schema";

export type TokenResponse = components["schemas"]["TokenResponse"];
export type AccountOut = components["schemas"]["AccountOut"];
export type PayeeOut = components["schemas"]["PayeeOut"];
export type TransferOut = components["schemas"]["TransferOut"];
export type UserOut = components["schemas"]["UserOut"];
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
  // Transfer bodies for 202 (pending) and 403 (declined) carry state the UI must show. They are
  // plain JSON; a problem+json 403 (e.g. a non-customer token) is an error, not a transfer.
  const isTransferState =
    path.startsWith("/api/v1/transfers") &&
    (response.status === 202 || response.status === 403) &&
    !isProblem(response);
  if (!response.ok && !isTransferState) throw await toError(response);
  return (response.status === 204 ? undefined : await response.json()) as T;
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
  payees: () => apiFetch<PayeeOut[]>("/api/v1/payees"),
  /** One Idempotency-Key per user intent; reuse the SAME key when retrying the same submit. */
  transfer: (
    body: { from_account_id: string; payee_id: string; amount_minor: number; reference?: string },
    idempotencyKey: string,
  ) =>
    apiFetch<TransferOut>("/api/v1/transfers", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    }),
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
  fraudAlerts: (status?: string) => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    return apiFetch<AlertSummary[]>(`/api/v1/fraud/alerts?${params.toString()}`);
  },
  fraudAlert: (alertId: string) =>
    apiFetch<AlertDetail>(`/api/v1/fraud/alerts/${encodeURIComponent(alertId)}`),
  decideAlert: (alertId: string, action: "RELEASE" | "REJECT", note: string) =>
    apiFetch<undefined>(`/api/v1/fraud/alerts/${encodeURIComponent(alertId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ action, note: note.trim() === "" ? null : note.trim() }),
    }),
  fraudStats: (days: number) => apiFetch<FraudStats>(`/api/v1/fraud/stats?days=${days}`),
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
