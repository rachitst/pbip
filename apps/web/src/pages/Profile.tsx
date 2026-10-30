import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { describeBrowser, getDeviceId } from "../lib/device";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

const MIN_PASSWORD_LENGTH = 12; // mirrors the server policy; the server remains the authority

export function Profile() {
  return (
    <div className="page">
      <PageHeader title="Profile & security" />
      <ProfileDetails />
      <Devices />
      <ChangePassword />
    </div>
  );
}

function ProfileDetails() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  if (profile.isPending) return <LoadingState label="Loading profile..." />;
  if (profile.isError) {
    return (
      <ErrorState
        message="Could not load your profile."
        onRetry={() => {
          void profile.refetch();
        }}
      />
    );
  }
  const p = profile.data;
  return (
    <section>
      <h2>Your details</h2>
      <dl className="details">
        <dt>Name</dt>
        <dd>{p.full_name}</dd>
        <dt>Email</dt>
        <dd>{p.email ?? "Not available"}</dd>
        <dt>Phone</dt>
        <dd>{p.phone_masked ?? "Not provided"}</dd>
        <dt>KYC status</dt>
        <dd>{p.kyc_status}</dd>
        <dt>Customer since</dt>
        <dd>{formatDate(p.created_at)}</dd>
      </dl>
    </section>
  );
}

function Devices() {
  const queryClient = useQueryClient();
  const devices = useQuery({ queryKey: ["devices"], queryFn: api.devices });
  const register = useMutation({
    mutationFn: () => api.registerDevice(getDeviceId(), describeBrowser()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["devices"] }),
  });

  return (
    <section>
      <h2>Your devices</h2>
      <p className="muted">
        Devices you sign in from. New devices are untrusted until they build history; this helps us
        spot sign-ins that are not you.
      </p>
      {devices.isPending && <LoadingState label="Loading devices..." />}
      {devices.isError && (
        <ErrorState
          message="Could not load your devices."
          onRetry={() => {
            void devices.refetch();
          }}
        />
      )}
      {devices.data?.length === 0 && <EmptyState message="No devices registered yet." />}
      <ul className="list">
        {devices.data?.map((device) => (
          <li key={device.id} className="row">
            <div>
              <strong>{device.label ?? "Unnamed device"}</strong>
              <div className="muted">
                First seen {formatDate(device.first_seen_at)} - last seen{" "}
                {formatDate(device.last_seen_at)}
              </div>
            </div>
            <span className={device.trusted ? "ok" : "muted"}>
              {device.trusted ? "Trusted" : `New (${device.device_age_days} days)`}
            </span>
          </li>
        ))}
      </ul>
      <Button
        variant="secondary"
        loading={register.isPending}
        onClick={() => {
          register.mutate();
        }}
      >
        {register.isPending ? "Registering..." : "Register this browser"}
      </Button>
      {register.isSuccess && <Alert tone="success">This browser is registered.</Alert>}
      {register.isError && (
        <Alert tone="error">
          {errorMessage(register.error, "Could not register this browser.")}
        </Alert>
      )}
    </section>
  );
}

function passwordError(error: unknown): string {
  if (error instanceof ApiError && error.code === "invalid_current_password") {
    return "Your current password is incorrect.";
  }
  // Policy messages (length, common/breached password) are server-written and safe to show.
  return errorMessage(error, "Could not change your password. Try again.");
}

function ChangePassword() {
  const { logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const change = useMutation({
    mutationFn: () => api.changePassword(current, next),
    // Every session (this one included) was revoked server-side: sign out and say why.
    onSuccess: () =>
      logout(
        "Password changed. All your sessions were signed out - sign in with your new password.",
      ),
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    setLocalError(null);
    if (next.length < MIN_PASSWORD_LENGTH) {
      setLocalError(`The new password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (next !== confirm) {
      setLocalError("The new passwords do not match.");
      return;
    }
    if (next === current) {
      setLocalError("The new password must be different from the current one.");
      return;
    }
    change.mutate();
  }

  const message = localError ?? (change.isError ? passwordError(change.error) : null);
  return (
    <section>
      <h2>Change password</h2>
      <p className="muted">Changing your password signs you out on every device.</p>
      <form onSubmit={submit}>
        <Field label="Current password">
          {(control) => (
            <input
              {...control}
              type="password"
              value={current}
              onChange={(event) => {
                setCurrent(event.target.value);
              }}
              autoComplete="current-password"
              required
            />
          )}
        </Field>
        <Field label="New password">
          {(control) => (
            <input
              {...control}
              type="password"
              value={next}
              onChange={(event) => {
                setNext(event.target.value);
              }}
              autoComplete="new-password"
              minLength={MIN_PASSWORD_LENGTH}
              required
            />
          )}
        </Field>
        <Field label="Confirm new password">
          {(control) => (
            <input
              {...control}
              type="password"
              value={confirm}
              onChange={(event) => {
                setConfirm(event.target.value);
              }}
              autoComplete="new-password"
              required
            />
          )}
        </Field>
        {message && <Alert tone="error">{message}</Alert>}
        <Button type="submit" loading={change.isPending}>
          {change.isPending ? "Changing..." : "Change password"}
        </Button>
      </form>
    </section>
  );
}
