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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
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
}

async function toError(response: Response): Promise<ApiError> {
  const problem = (await response.json().catch(() => ({}))) as Problem;
  return new ApiError(
    response.status,
    problem.code ?? "error",
    problem.title ?? response.statusText,
  );
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
  // Transfer bodies for 202 (pending) and 403 (declined) carry state the UI must show.
  const isTransferState =
    path.startsWith("/api/v1/transfers") && (response.status === 202 || response.status === 403);
  if (!response.ok && !isTransferState) throw await toError(response);
  return (response.status === 204 ? undefined : await response.json()) as T;
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
};
