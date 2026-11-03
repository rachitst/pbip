import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../../api/client";
import type { LockoutStatusOut, UnlockResultOut, UserOut } from "../../api/client";
import { ROLE_LABELS, STAFF_ROLES } from "../../auth/session";
import type { StaffRole } from "../../auth/session";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime } from "../../lib/format";
import { Alert } from "../../ui/Alert";
import { Button } from "../../ui/Button";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { Field } from "../../ui/Field";
import { ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** POST /admin/users: a staff account with roles the admin picks explicitly (none preselected).
 * The password policy is the server's; the new user enrols an authenticator at first sign-in. */
export function CreateStaffUser() {
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [roles, setRoles] = useState<StaffRole[]>([]);
  const [created, setCreated] = useState<UserOut | null>(null);
  const create = useMutation({
    mutationFn: () =>
      api.adminCreateUser({ email: email.trim(), full_name: fullName.trim(), password, roles }),
    onSuccess: (user) => {
      setCreated(user);
      setEmail("");
      setFullName("");
      setPassword("");
      setRoles([]);
    },
  });
  const ready =
    email.trim().length > 3 &&
    fullName.trim().length >= 2 &&
    password.length > 0 &&
    roles.length > 0;

  function toggle(role: StaffRole, on: boolean) {
    setRoles((current) => (on ? [...current, role] : current.filter((r) => r !== role)));
  }
  function submit(event: SyntheticEvent) {
    event.preventDefault();
    setCreated(null);
    if (ready) create.mutate();
  }

  return (
    <section className="card admin-panel" aria-labelledby="admin-create-user">
      <h2 id="admin-create-user">Create a staff user</h2>
      <form onSubmit={submit} noValidate>
        <div className="field-row">
          <Field label="Work e-mail">
            {(control) => (
              <input
                {...control}
                type="email"
                autoComplete="off"
                maxLength={254}
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                }}
              />
            )}
          </Field>
          <Field label="Full name">
            {(control) => (
              <input
                {...control}
                autoComplete="off"
                maxLength={200}
                value={fullName}
                onChange={(e) => {
                  setFullName(e.target.value);
                }}
              />
            )}
          </Field>
        </div>
        <Field
          label="Initial password"
          hint="Checked against the bank's password policy. Share it through an approved channel."
        >
          {(control) => (
            <input
              {...control}
              type="password"
              autoComplete="new-password"
              maxLength={256}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
              }}
            />
          )}
        </Field>
        <fieldset className="choice-group admin-roles">
          <legend>Roles (choose at least one)</legend>
          {STAFF_ROLES.map((role) => (
            <label key={role}>
              <input
                type="checkbox"
                checked={roles.includes(role)}
                onChange={(e) => {
                  toggle(role, e.target.checked);
                }}
              />{" "}
              {ROLE_LABELS[role] ?? role}
            </label>
          ))}
        </fieldset>
        {create.isError && (
          <Alert tone="error">{errorMessage(create.error, "Could not create the user.")}</Alert>
        )}
        {created && (
          <Alert tone="success">
            Created {created.email} as{" "}
            {created.roles.map((role) => ROLE_LABELS[role] ?? role).join(", ")}. User id{" "}
            <code className="admin-id">{created.id}</code>. They must set up an authenticator before
            using staff tools.
          </Alert>
        )}
        <div className="admin-actions">
          <Button type="submit" loading={create.isPending} disabled={!ready}>
            Create user
          </Button>
        </div>
      </form>
    </section>
  );
}

function LockoutDetails({ status }: { status: LockoutStatusOut }) {
  return (
    <dl className="details">
      <div className="details__row">
        <dt>Status</dt>
        <dd>
          <StatusBadge
            status={
              status.is_locked
                ? { label: "Locked", tone: "danger" }
                : { label: "Not locked", tone: "success" }
            }
          />
        </dd>
      </div>
      <div className="details__row">
        <dt>Failed sign-ins</dt>
        <dd>{status.failed_login_count}</dd>
      </div>
      <div className="details__row">
        <dt>Locked until</dt>
        <dd>{status.locked_until ? formatDateTime(status.locked_until) : "-"}</dd>
      </div>
      <div className="details__row">
        <dt>Last failure</dt>
        <dd>{status.last_failure_at ? formatDateTime(status.last_failure_at) : "-"}</dd>
      </div>
    </dl>
  );
}

function unlockSummary(result: UnlockResultOut): string {
  const was = result.was_locked ? "The lockout was cleared" : "The user was not locked";
  const counters =
    result.rate_limit_counters_cleared === null
      ? "the sign-in rate-limit counters could not be reached"
      : `${countOf(result.rate_limit_counters_cleared, "rate-limit counter")} removed`;
  return `${was}; ${countOf(result.previous_failed_login_count, "failed sign-in")} reset, ${counters}.`;
}

/** GET /admin/users/{id}/lockout and POST .../unlock. The unlock is confirmed first and audited by
 * the server with the admin's id. */
export function LockoutPanel() {
  const queryClient = useQueryClient();
  const [input, setInput] = useState("");
  const [userId, setUserId] = useState<string | null>(null);
  const [inputError, setInputError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<UnlockResultOut | null>(null);
  const status = useQuery({
    queryKey: ["admin", "lockout", userId],
    queryFn: () => api.adminLockout(userId ?? ""),
    enabled: userId !== null,
  });
  const unlock = useMutation({
    mutationFn: (id: string) => api.adminUnlock(id),
    onSuccess: (unlocked) => {
      setConfirming(false);
      setResult(unlocked);
      queryClient.setQueryData(["admin", "lockout", unlocked.user_id], unlocked);
    },
  });

  function inspect(event: SyntheticEvent) {
    event.preventDefault();
    const id = input.trim();
    if (!UUID_PATTERN.test(id)) {
      setInputError("Enter the user id in the form 123e4567-e89b-42d3-a456-426614174000.");
      return;
    }
    setInputError(null);
    setResult(null);
    unlock.reset();
    setUserId(id.toLowerCase());
  }

  const data = status.data;
  return (
    <section className="card admin-panel" aria-labelledby="admin-lockout">
      <h2 id="admin-lockout">Sign-in lockout</h2>
      <form onSubmit={inspect} className="admin-inline-form" noValidate>
        <Field label="User id" error={inputError}>
          {(control) => (
            <input
              {...control}
              autoComplete="off"
              spellCheck={false}
              maxLength={36}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
              }}
            />
          )}
        </Field>
        <Button type="submit" variant="secondary">
          Inspect
        </Button>
      </form>
      {userId === null && (
        <p className="muted">Enter a user id to see their failed sign-ins and lockout.</p>
      )}
      {userId !== null && status.isPending && <LoadingState label="Loading lockout status..." />}
      {status.isError && (
        <ErrorState
          message={errorMessage(status.error, "Could not load the lockout status.")}
          onRetry={() => {
            void status.refetch();
          }}
        />
      )}
      {data && (
        <>
          <LockoutDetails status={data} />
          {result && <Alert tone="success">{unlockSummary(result)}</Alert>}
          {(data.is_locked || data.failed_login_count > 0) && (
            <div className="admin-actions">
              <Button
                variant="danger"
                onClick={() => {
                  unlock.reset();
                  setConfirming(true);
                }}
              >
                Clear lockout
              </Button>
            </div>
          )}
        </>
      )}
      <ConfirmDialog
        open={confirming}
        title="Clear this lockout?"
        tone="danger"
        items={[
          { label: "User id", value: <code className="admin-id">{userId}</code> },
          { label: "Failed sign-ins", value: data?.failed_login_count ?? 0 },
          {
            label: "Effect",
            value:
              "Resets failed sign-ins and the sign-in rate limit. Recorded in the audit log with your name.",
          },
        ]}
        confirmLabel="Clear lockout"
        busy={unlock.isPending}
        error={unlock.isError ? errorMessage(unlock.error, "Could not clear the lockout.") : null}
        onConfirm={() => {
          if (userId) unlock.mutate(userId);
        }}
        onCancel={() => {
          setConfirming(false);
        }}
      />
    </section>
  );
}
