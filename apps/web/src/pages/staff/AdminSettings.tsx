import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../../api/client";
import type { RuntimeSettingOut } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDateTime, formatRelative } from "../../lib/format";
import { formatMinor, parseToMinor } from "../../lib/money";
import { Alert } from "../../ui/Alert";
import { Button } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { Field } from "../../ui/Field";
import { MoneyInput } from "../../ui/MoneyInput";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";

export const MIN_REASON = 10;
export const MAX_REASON = 500;

/** Shows a setting's value in its unit: money as INR from integer minor units, never a float. */
export function formatSettingValue(setting: RuntimeSettingOut, value: number): string {
  return setting.unit === "MINOR_UNITS" ? formatMinor(value) : String(value);
}

/** The typed text as the setting's integer value, or null if it is not one. */
export function parseSettingValue(setting: RuntimeSettingOut, text: string): number | null {
  if (setting.unit === "MINOR_UNITS") return parseToMinor(text);
  const cleaned = text.trim();
  return /^\d{1,9}$/.test(cleaned) ? Number(cleaned) : null;
}

/** Client-side check mirroring the server's allow-listed bounds (the server re-checks). */
export function valueError(setting: RuntimeSettingOut, text: string): string | null {
  const value = parseSettingValue(setting, text);
  const range = `${formatSettingValue(setting, setting.min_value)} to ${formatSettingValue(setting, setting.max_value)}`;
  if (value === null) {
    return setting.unit === "MINOR_UNITS"
      ? "Enter an amount with at most two decimals."
      : "Enter a whole number.";
  }
  if (value < setting.min_value || value > setting.max_value) return `Allowed range: ${range}.`;
  if (value === setting.effective_value) return "This is already the value in force.";
  return null;
}

function toInputText(setting: RuntimeSettingOut): string {
  const value = setting.effective_value;
  if (setting.unit !== "MINOR_UNITS") return String(value);
  return `${String(Math.trunc(value / 100))}.${String(value % 100).padStart(2, "0")}`;
}

function ChangeDialog({
  setting,
  onClose,
  onChanged,
}: {
  setting: RuntimeSettingOut;
  onClose: () => void;
  onChanged: (updated: RuntimeSettingOut) => void;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(() => toInputText(setting));
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const change = useMutation({
    mutationFn: (value: number) =>
      api.changeRuntimeSetting(setting.key, { value, reason: reason.trim() }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ["admin", "settings"] });
      onChanged(updated);
    },
  });
  const problem = valueError(setting, text);
  const trimmed = reason.trim();
  const reasonOk = trimmed.length >= MIN_REASON && trimmed.length <= MAX_REASON;

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    setTouched(true);
    const value = parseSettingValue(setting, text);
    if (problem === null && reasonOk && value !== null) change.mutate(value);
  }
  const close = () => {
    if (!change.isPending) onClose();
  };
  return (
    <Dialog open title={`Change ${setting.label.toLowerCase()}`} onClose={close}>
      <form onSubmit={submit} noValidate>
        <p className="muted">
          {setting.description} In force now: {formatSettingValue(setting, setting.effective_value)}
          ; environment default: {formatSettingValue(setting, setting.default_value)}.
        </p>
        <Field
          label="New value"
          hint={`Allowed: ${formatSettingValue(setting, setting.min_value)} to ${formatSettingValue(setting, setting.max_value)}. Applies to the next payment.`}
          error={touched ? problem : null}
        >
          {(control) =>
            setting.unit === "MINOR_UNITS" ? (
              <MoneyInput {...control} currency="INR" value={text} onValueChange={setText} />
            ) : (
              <input
                {...control}
                inputMode="numeric"
                autoComplete="off"
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                }}
              />
            )
          }
        </Field>
        <Field
          label="Reason"
          hint={`Recorded in the audit log with your name, the old and the new value; ${String(MIN_REASON)} to ${String(MAX_REASON)} characters.`}
          error={
            touched && !reasonOk
              ? `Give a reason of at least ${String(MIN_REASON)} characters.`
              : null
          }
        >
          {(control) => (
            <textarea
              {...control}
              rows={3}
              maxLength={MAX_REASON}
              value={reason}
              onChange={(e) => {
                setReason(e.target.value);
              }}
            />
          )}
        </Field>
        {change.isError && (
          <Alert tone="error">{errorMessage(change.error, "Could not change the setting.")}</Alert>
        )}
        <div className="dialog__actions">
          <Button variant="ghost" onClick={close} disabled={change.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={change.isPending}>
            Save change
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function SettingsTable({
  items,
  onChange,
}: {
  items: RuntimeSettingOut[];
  onChange: (setting: RuntimeSettingOut) => void;
}) {
  return (
    <div className="entries admin-settings-scroll">
      <table className="admin-settings-table">
        <caption className="sr-only">Runtime-configurable settings</caption>
        <thead>
          <tr>
            <th scope="col">Setting</th>
            <th scope="col" className="right">
              In force
            </th>
            <th scope="col" className="right">
              Default
            </th>
            <th scope="col">Allowed range</th>
            <th scope="col">Last change</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((setting) => (
            <tr key={setting.key}>
              <th scope="row">
                {setting.label}
                <span className="admin-setting-description">{setting.description}</span>
              </th>
              <td className="right">
                {formatSettingValue(setting, setting.effective_value)}{" "}
                <StatusBadge
                  status={
                    setting.overridden
                      ? { label: "Changed", tone: "warning" }
                      : { label: "Default", tone: "neutral" }
                  }
                />
              </td>
              <td className="right">{formatSettingValue(setting, setting.default_value)}</td>
              <td>
                {formatSettingValue(setting, setting.min_value)} to{" "}
                {formatSettingValue(setting, setting.max_value)}
              </td>
              <td>
                {setting.updated_at ? (
                  <span title={formatDateTime(setting.updated_at)}>
                    {formatRelative(setting.updated_at)}
                  </span>
                ) : (
                  <span className="muted">Never</span>
                )}
              </td>
              <td className="row-actions">
                <Button
                  size="sm"
                  variant="secondary"
                  aria-label={`Change ${setting.label.toLowerCase()}`}
                  onClick={() => {
                    onChange(setting);
                  }}
                >
                  Change
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** GET /admin/settings and PUT /admin/settings/{key} (ADR-0031): only the allow-listed business
 * limits appear; secrets and security settings are not configurable here or anywhere at runtime. */
export function RuntimeSettingsPanel() {
  const [editing, setEditing] = useState<RuntimeSettingOut | null>(null);
  const [saved, setSaved] = useState<RuntimeSettingOut | null>(null);
  const query = useQuery({ queryKey: ["admin", "settings"], queryFn: api.runtimeSettings });
  const items = query.data?.items;
  return (
    <section className="card admin-panel" aria-labelledby="admin-settings">
      <h2 id="admin-settings">Application settings</h2>
      <p className="muted">
        Business limits that may change without a deployment. Secrets, sign-in and security settings
        are never configurable here. Every change is recorded in the audit log.
      </p>
      {saved && (
        <Alert tone="success">
          {saved.label} is now {formatSettingValue(saved, saved.effective_value)}. It applies to the
          next payment.
        </Alert>
      )}
      {query.isPending && <LoadingState label="Loading settings..." />}
      {query.isError && (
        <ErrorState
          message={errorMessage(query.error, "Could not load the settings.")}
          onRetry={() => {
            void query.refetch();
          }}
        />
      )}
      {items && items.length === 0 && (
        <EmptyState message="No settings can be changed at runtime." />
      )}
      {items && items.length > 0 && (
        <SettingsTable
          items={items}
          onChange={(setting) => {
            setSaved(null);
            setEditing(setting);
          }}
        />
      )}
      {editing && (
        <ChangeDialog
          key={editing.key}
          setting={editing}
          onClose={() => {
            setEditing(null);
          }}
          onChanged={(updated) => {
            setEditing(null);
            setSaved(updated);
          }}
        />
      )}
    </section>
  );
}
