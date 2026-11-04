import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { CustomerProfileOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { ErrorState, LoadingState } from "../ui/States";
import "./PhoneSection.css";

const QUERY_KEY = ["profile"];
/** E.164, as the API checks it: a plus sign, then 8 to 15 digits, the first not 0. */
const E164 = /^\+[1-9][0-9]{7,14}$/;

/** Spaces, dashes, dots and brackets people type are dropped; everything else is kept so the
 * check below can refuse it. */
export function normalisePhone(input: string): string {
  return input.replace(/[\s\-.()]/g, "");
}

export function PhoneSection() {
  const profile = useQuery({ queryKey: QUERY_KEY, queryFn: api.profile });
  return (
    <section aria-labelledby="phone-heading">
      <h2 id="phone-heading">Mobile number</h2>
      <p className="muted">
        Optional, shown under Your details. We store it encrypted and only ever show its last four
        digits. It is not used to sign in or to reset your password.
      </p>
      {profile.isPending && <LoadingState label="Loading your mobile number..." />}
      {profile.isError && (
        <ErrorState
          message="Could not load your mobile number."
          onRetry={() => {
            void profile.refetch();
          }}
        />
      )}
      {profile.data && <PhoneForm current={profile.data} />}
    </section>
  );
}

function PhoneForm({ current }: { current: CustomerProfileOut }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const save = useMutation({
    mutationFn: api.setPhone,
    onSuccess: (saved) => {
      setValue("");
      queryClient.setQueryData(QUERY_KEY, saved);
    },
  });
  const remove = useMutation({
    mutationFn: api.deletePhone,
    onSuccess: async () => {
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    setLocalError(null);
    const phone = normalisePhone(value);
    if (!E164.test(phone)) {
      setLocalError("Enter the number with its country code, for example +91 98123 45678.");
      return;
    }
    save.mutate(phone);
  }

  const error =
    localError ?? (save.isError ? errorMessage(save.error, "Could not save your number.") : null);
  return (
    <form onSubmit={submit}>
      <Field
        label={current.phone_masked ? "New number" : "Number"}
        hint="Include the country code, for example +91 98123 45678."
      >
        {(control) => (
          <input
            {...control}
            className="phone-form__input"
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            maxLength={24}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
            }}
            required
          />
        )}
      </Field>
      {error && <Alert tone="error">{error}</Alert>}
      {save.isSuccess && <Alert tone="success">Mobile number saved.</Alert>}
      {remove.isSuccess && <Alert tone="success">Mobile number deleted.</Alert>}
      <div className="form-actions">
        <Button type="submit" loading={save.isPending} disabled={!value.trim()}>
          {save.isPending ? "Saving..." : "Save mobile number"}
        </Button>
        {current.phone_masked && (
          <Button
            variant="danger"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Delete mobile number
          </Button>
        )}
      </div>
      <ConfirmDialog
        open={confirming}
        title="Delete your mobile number?"
        items={[{ label: "Effect", value: "We will no longer hold a mobile number for you." }]}
        confirmLabel="Delete"
        tone="danger"
        busy={remove.isPending}
        error={remove.isError ? errorMessage(remove.error, "Could not delete your number.") : null}
        onConfirm={() => {
          remove.mutate();
        }}
        onCancel={() => {
          setConfirming(false);
        }}
      />
    </form>
  );
}
