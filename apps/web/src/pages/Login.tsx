import { useState } from "react";
import type { SyntheticEvent } from "react";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../lib/errors";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { AuthFrame } from "./AuthFrame";

/** ``onRegister`` is given only where self-registration is offered (the signed-out app);
 * ``embedded`` puts the form in the landing page's hero instead of a page of its own. */
export function Login({
  onRegister,
  embedded = false,
}: {
  onRegister?: () => void;
  embedded?: boolean;
}) {
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
      setError(errorMessage(caught, "Something went wrong. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthFrame title="Sign in" embedded={embedded}>
      {notice && <Alert tone="success">{notice}</Alert>}
      <form className="auth-form" onSubmit={(event) => void submit(event)}>
        {mfaToken ? (
          <>
            <p className="auth-form__lead">
              Enter the 6-digit code from your authenticator app, or one of your recovery codes.
            </p>
            <Field label="Authenticator or recovery code">
              {(control) => (
                <input
                  {...control}
                  value={code}
                  onChange={(event) => {
                    setCode(event.target.value);
                  }}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  autoFocus
                  required
                />
              )}
            </Field>
          </>
        ) : (
          <>
            <Field label="Email">
              {(control) => (
                <input
                  {...control}
                  type="email"
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value);
                  }}
                  autoComplete="username"
                  required
                />
              )}
            </Field>
            <Field label="Password">
              {(control) => (
                <input
                  {...control}
                  type="password"
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                  }}
                  autoComplete="current-password"
                  required
                />
              )}
            </Field>
          </>
        )}
        {error && <Alert tone="error">{error}</Alert>}
        <Button type="submit" className="auth-form__submit" loading={busy}>
          {mfaToken ? "Verify" : "Sign in"}
        </Button>
      </form>
      {onRegister && !mfaToken && (
        <p className="auth-card__switch">
          New to Finora?{" "}
          <Button variant="link" onClick={onRegister}>
            Create an account
          </Button>
        </p>
      )}
    </AuthFrame>
  );
}
