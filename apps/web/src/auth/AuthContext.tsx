import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, setAccessToken, setSessionLostHandler } from "../api/client";

type Status = "loading" | "anonymous" | "authenticated";

interface AuthState {
  status: Status;
  /** Non-null while the password was accepted but a TOTP code is still required. */
  mfaToken: string | null;
  login: (email: string, password: string) => Promise<void>;
  verifyMfa: (code: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [mfaToken, setMfaToken] = useState<string | null>(null);

  useEffect(() => {
    setSessionLostHandler(() => {
      setStatus("anonymous");
    });
    // Restore a session after reload from the HttpOnly refresh cookie (if any).
    void api.restoreSession().then((ok) => {
      setStatus(ok ? "authenticated" : "anonymous");
    });
    return () => {
      setSessionLostHandler(null);
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.login(email, password);
    if (result.mfa_required && result.mfa_token) {
      setMfaToken(result.mfa_token);
      return;
    }
    setAccessToken(result.access_token ?? null);
    setStatus("authenticated");
  }, []);

  const verifyMfa = useCallback(
    async (code: string) => {
      if (!mfaToken) return;
      const result = await api.verifyMfa(mfaToken, code);
      setAccessToken(result.access_token ?? null);
      setMfaToken(null);
      setStatus("authenticated");
    },
    [mfaToken],
  );

  const logout = useCallback(async () => {
    await api.logout().catch(() => undefined);
    setAccessToken(null);
    setMfaToken(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, mfaToken, login, verifyMfa, logout }),
    [status, mfaToken, login, verifyMfa, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
