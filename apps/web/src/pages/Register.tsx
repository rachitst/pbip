import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../lib/errors";
import { MIN_PASSWORD_LENGTH, newPasswordProblem } from "../lib/password";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";

/** Only the API's own wording is shown: it never echoes input and says no more than the API does. */
function registrationError(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "Online registration is not available. Visit a branch to open an account.";
  }
  return errorMessage(error, "Could not create your account. Try again.");
}

/** #201: self-registration (POST /auth/register), then the normal sign-in with the same details. */
export function Register({ onSignIn }: { onSignIn: () => void }) {
  const { login } = useAuth();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    const problem = newPasswordProblem(password, confirm);
    setError(problem);
    if (problem) return;
    setBusy(true);
    try {
      await api.register(fullName.trim(), email.trim(), password);
    } catch (caught) {
      setError(registrationError(caught));
      setBusy(false);
      return;
    }
    try {
      await login(email.trim(), password);
    } catch (caught) {
      // The account exists; only the automatic sign-in failed (e.g. rate limit). Say so.
      setError(
        `Your account was created, but we could not sign you in: ${errorMessage(caught, "try signing in.")}`,
      );
      setBusy(false);
    }
  }

  return (
    <main className="card narrow">
      <PageHeader title="Create your account" />
      <form onSubmit={(event) => void submit(event)}>
        <Field label="Full name">
          {(control) => (
            <input
              {...control}
              value={fullName}
              onChange={(event) => {
                setFullName(event.target.value);
              }}
              autoComplete="name"
              minLength={2}
              maxLength={200}
              required
            />
          )}
        </Field>
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
              maxLength={254}
              required
            />
          )}
        </Field>
        <Field label="Password" hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}>
          {(control) => (
            <input
              {...control}
              type="password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              autoComplete="new-password"
              minLength={MIN_PASSWORD_LENGTH}
              maxLength={256}
              required
            />
          )}
        </Field>
        <Field label="Confirm password">
          {(control) => (
            <input
              {...control}
              type="password"
              value={confirm}
              onChange={(event) => {
                setConfirm(event.target.value);
              }}
              autoComplete="new-password"
              maxLength={256}
              required
            />
          )}
        </Field>
        {error && <Alert tone="error">{error}</Alert>}
        <Button type="submit" loading={busy}>
          Create account
        </Button>
      </form>
      <p>
        Already have an account?{" "}
        <Button variant="link" onClick={onSignIn}>
          Sign in
        </Button>
      </p>
    </main>
  );
}
