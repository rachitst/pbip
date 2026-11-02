import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import { errorMessage } from "../lib/errors";
import { useCurrentUser } from "../shell/MeContext";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";

/** "JBSWY3DPEHPK3PXP" -> "JBSW Y3DP EHPK 3PXP": easier to type into an authenticator app. */
export function groupSecret(secret: string): string {
  return secret.match(/.{1,4}/g)?.join(" ") ?? secret;
}

function RecoveryCodes({ codes }: { codes: string[] }) {
  return (
    <>
      <Alert tone="success">
        Authenticator app set up. From your next sign-in, PBIP will ask for a code from the app
        after your password.
      </Alert>
      <p>
        <strong>Save these recovery codes now. They are shown only once.</strong> Each one works a
        single time in place of an app code if you lose your phone.
      </p>
      <ul className="recovery-codes" aria-label="Recovery codes">
        {codes.map((c) => (
          <li key={c}>
            <code>{c}</code>
          </li>
        ))}
      </ul>
    </>
  );
}

/** #201: TOTP enrolment (POST /auth/mfa/enroll, then /auth/mfa/confirm with a code). */
export function MfaEnrolment() {
  const me = useCurrentUser();
  const queryClient = useQueryClient();
  const [code, setCode] = useState("");
  const enroll = useMutation({ mutationFn: api.enrollMfa });
  const confirm = useMutation({
    mutationFn: (value: string) => api.confirmMfa(value),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    confirm.mutate(code.trim());
  }

  let body;
  if (confirm.data) {
    body = <RecoveryCodes codes={confirm.data.recovery_codes} />;
  } else if (me.mfa_enabled) {
    body = (
      <p>
        An authenticator app is set up. Every sign-in asks for a code from it after your password.
      </p>
    );
  } else if (!enroll.data) {
    body = (
      <>
        <p>
          Add a second step to sign-in: a 6-digit code from an authenticator app on your phone.
          {me.roles.includes("CUSTOMER") ? "" : " Staff tools need it."}
        </p>
        {enroll.isError && (
          <Alert tone="error">{errorMessage(enroll.error, "Could not start the set-up.")}</Alert>
        )}
        <Button
          loading={enroll.isPending}
          onClick={() => {
            enroll.mutate();
          }}
        >
          Set up authenticator app
        </Button>
      </>
    );
  } else {
    body = (
      <>
        <ol className="steps">
          <li>
            In your authenticator app, add an account and enter this key:{" "}
            <code className="secret">{groupSecret(enroll.data.secret)}</code>. On this phone you can
            instead <a href={enroll.data.otpauth_uri}>open it in the app</a>.
          </li>
          <li>Enter the 6-digit code the app now shows.</li>
        </ol>
        <form onSubmit={submit}>
          <Field label="Code from the app">
            {(control) => (
              <input
                {...control}
                value={code}
                onChange={(event) => {
                  setCode(event.target.value);
                }}
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6,8}"
                maxLength={8}
                required
              />
            )}
          </Field>
          {confirm.isError && (
            <Alert tone="error">
              {errorMessage(confirm.error, "Could not confirm the code. Try again.")}
            </Alert>
          )}
          <Button type="submit" loading={confirm.isPending}>
            Confirm
          </Button>
        </form>
      </>
    );
  }
  return (
    <section aria-labelledby="mfa-heading">
      <h2 id="mfa-heading">Two-step sign-in</h2>
      {body}
    </section>
  );
}
