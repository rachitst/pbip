import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { describeBrowser, getDeviceId } from "../lib/device";

const MIN_PASSWORD_LENGTH = 12; // mirrors the server policy; the server remains the authority

const dateFormat = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium" });

export function Profile() {
  return (
    <main>
      <h1>Profile &amp; security</h1>
      <ProfileDetails />
      <Devices />
      <ChangePassword />
    </main>
  );
}

function ProfileDetails() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: api.profile });
  if (profile.isPending) return <p>Loading profile...</p>;
  if (profile.isError) {
    return (
      <p role="alert" className="error">
        Could not load your profile.
      </p>
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
        <dd>{dateFormat.format(new Date(p.created_at))}</dd>
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
      {devices.isPending && <p>Loading devices...</p>}
      {devices.isError && (
        <p role="alert" className="error">
          Could not load your devices.
        </p>
      )}
      {devices.data?.length === 0 && <p className="muted">No devices registered yet.</p>}
      <ul className="list">
        {devices.data?.map((device) => (
          <li key={device.id} className="row">
            <div>
              <strong>{device.label ?? "Unnamed device"}</strong>
              <div className="muted">
                First seen {dateFormat.format(new Date(device.first_seen_at))} - last seen{" "}
                {dateFormat.format(new Date(device.last_seen_at))}
              </div>
            </div>
            <span className={device.trusted ? "ok" : "muted"}>
              {device.trusted ? "Trusted" : `New (${device.device_age_days} days)`}
            </span>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="secondary"
        disabled={register.isPending}
        onClick={() => {
          register.mutate();
        }}
      >
        {register.isPending ? "Registering..." : "Register this browser"}
      </button>
      {register.isSuccess && (
        <p role="status" className="ok">
          This browser is registered.
        </p>
      )}
      {register.isError && (
        <p role="alert" className="error">
          Could not register this browser.
        </p>
      )}
    </section>
  );
}

function passwordError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Could not change your password. Try again.";
  if (error.code === "invalid_current_password") return "Your current password is incorrect.";
  if (error.status === 423) return "Too many failed attempts. Your account is temporarily locked.";
  if (error.status === 429) return "Too many requests. Wait a minute and try again.";
  return error.message; // policy messages (length, common/breached password) are safe to show
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
        <label>
          Current password
          <input
            type="password"
            value={current}
            onChange={(event) => {
              setCurrent(event.target.value);
            }}
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          New password
          <input
            type="password"
            value={next}
            onChange={(event) => {
              setNext(event.target.value);
            }}
            autoComplete="new-password"
            minLength={MIN_PASSWORD_LENGTH}
            required
          />
        </label>
        <label>
          Confirm new password
          <input
            type="password"
            value={confirm}
            onChange={(event) => {
              setConfirm(event.target.value);
            }}
            autoComplete="new-password"
            required
          />
        </label>
        {message && (
          <p role="alert" className="error">
            {message}
          </p>
        )}
        <button type="submit" disabled={change.isPending}>
          {change.isPending ? "Changing..." : "Change password"}
        </button>
      </form>
    </section>
  );
}
