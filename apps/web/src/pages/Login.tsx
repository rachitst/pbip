import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function Login() {
  const { login, verifyMfa, mfaToken, notice } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mfaToken) await verifyMfa(code.trim());
      else await login(email.trim(), password);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="card narrow">
      <h1>Sign in</h1>
      {notice && (
        <p role="status" className="ok">
          {notice}
        </p>
      )}
      <form onSubmit={(event) => void submit(event)}>
        {mfaToken ? (
          <label>
            Authenticator or recovery code
            <input
              value={code}
              onChange={(event) => {
                setCode(event.target.value);
              }}
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              required
            />
          </label>
        ) : (
          <>
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                }}
                autoComplete="username"
                required
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
                autoComplete="current-password"
                required
              />
            </label>
          </>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {mfaToken ? "Verify" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
