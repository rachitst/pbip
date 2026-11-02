import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { AccountOut, PayeeOut, ScheduledTransferOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel, formatDate } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import type { Tone } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

type Frequency = ScheduledTransferOut["frequency"];
const KEY = ["scheduled-transfers"];
const DAY_MS = 86_400_000;
const MAX_START_DAYS = 366;

export const FREQUENCY: Record<Frequency, string> = {
  DAILY: "Every day",
  WEEKLY: "Every week",
  MONTHLY: "Every month",
};

const STATUS: Record<ScheduledTransferOut["status"], { label: string; tone: Tone }> = {
  ACTIVE: { label: "Active", tone: "success" },
  PAUSED: { label: "Paused", tone: "warning" },
  CANCELLED: { label: "Cancelled", tone: "neutral" },
  COMPLETED: { label: "Finished", tone: "neutral" },
};

const FAILURE: Record<string, string> = {
  insufficient_funds: "There was not enough money in the account.",
  daily_limit_exceeded: "It would have gone over your daily transfer limit.",
};

/** One sentence about anything the customer should know or do. Never shows fraud reasons. */
export function statusNote(order: ScheduledTransferOut): string | null {
  if (order.status === "PAUSED") {
    if (order.pause_reason === "FRAUD_BLOCK") {
      return "Stopped for your protection after a payment was blocked. Contact the bank; you can cancel it here.";
    }
    if (order.pause_reason === "CUSTOMER_ACTION_REQUIRED") {
      return "Paused because a payment could not be made. Check the payee and account, then resume.";
    }
    return "Paused by you. No payments are made until you resume.";
  }
  if (order.status === "ACTIVE" && order.last_failure_reason) {
    return `The last payment did not go through: ${FAILURE[order.last_failure_reason] ?? "it could not be made."}`;
  }
  return null;
}

/** YYYY-MM-DD in India (the bank's day) for an instant. */
export function istDate(at: number): string {
  return new Date(at + 5.5 * 3_600_000).toISOString().slice(0, 10);
}

interface FormState {
  from: string;
  to: string; // "payee:<id>" or "account:<id>"
  amount: string;
  frequency: Frequency;
  start: string;
  end: string;
  reference: string;
}

export function formProblem(f: FormState, today: string): string | null {
  if (!f.from || !f.to) return "Choose the account to pay from and who to pay.";
  if (f.to === `account:${f.from}`) return "Choose a different account to pay into.";
  const minor = parseToMinor(f.amount);
  if (minor === null || minor <= 0) return "Enter an amount like 1500 or 1500.50.";
  if (!f.start || f.start < today) return "The first payment must be today or later.";
  if ((Date.parse(f.start) - Date.parse(today)) / DAY_MS > MAX_START_DAYS) {
    return "The first payment must be within a year.";
  }
  if (f.end && f.end < f.start) return "The last date must be on or after the first payment.";
  return null;
}

function NewSchedule({
  accounts,
  payees,
  today,
  onDone,
}: {
  accounts: AccountOut[];
  payees: PayeeOut[];
  today: string;
  onDone: (created: ScheduledTransferOut | null) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>({
    from: accounts[0]?.id ?? "",
    to: "",
    amount: "",
    frequency: "MONTHLY",
    start: today,
    end: "",
    reference: "",
  });
  const [problem, setProblem] = useState<string | null>(null);
  const [intentKey] = useState(() => crypto.randomUUID());
  const create = useMutation({
    mutationFn: () => {
      const [kind, id = ""] = form.to.split(":");
      return api.createScheduledTransfer(
        {
          from_account_id: form.from,
          amount_minor: parseToMinor(form.amount) ?? 0,
          currency: "INR",
          frequency: form.frequency,
          start_date: form.start,
          end_date: form.end || null,
          reference: form.reference.trim() || null,
          ...(kind === "payee" ? { payee_id: id } : { to_account_id: id }),
        },
        intentKey,
      );
    },
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: KEY });
      onDone(created);
    },
  });
  const set = (key: keyof FormState) => (value: string) => {
    setForm({ ...form, [key]: value });
  };
  function submit(event: SyntheticEvent) {
    event.preventDefault();
    const found = formProblem(form, today);
    setProblem(found);
    if (!found) create.mutate();
  }
  const ownTargets = accounts.filter((a) => a.id !== form.from);
  const message =
    problem ?? (create.isError ? errorMessage(create.error, "Could not schedule it.") : null);
  return (
    <form onSubmit={submit}>
      <Field label="Pay from">
        {(control) => (
          <select
            {...control}
            value={form.from}
            onChange={(e) => {
              set("from")(e.target.value);
            }}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {accountTypeLabel(a.account_type)} {a.account_number_masked}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="Pay to" hint="Only verified payees can be paid.">
        {(control) => (
          <select
            {...control}
            value={form.to}
            onChange={(e) => {
              set("to")(e.target.value);
            }}
          >
            <option value="">Choose who to pay</option>
            {payees.length > 0 && (
              <optgroup label="Payees">
                {payees.map((p) => (
                  <option key={p.id} value={`payee:${p.id}`}>
                    {p.name} ({p.account_number_masked})
                  </option>
                ))}
              </optgroup>
            )}
            {ownTargets.length > 0 && (
              <optgroup label="My accounts">
                {ownTargets.map((a) => (
                  <option key={a.id} value={`account:${a.id}`}>
                    {accountTypeLabel(a.account_type)} {a.account_number_masked}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        )}
      </Field>
      <Field label="Amount (INR)">
        {(control) => (
          <MoneyInput
            {...control}
            value={form.amount}
            onValueChange={set("amount")}
            currency="INR"
          />
        )}
      </Field>
      <Field label="How often">
        {(control) => (
          <select
            {...control}
            value={form.frequency}
            onChange={(e) => {
              set("frequency")(e.target.value);
            }}
          >
            {(Object.keys(FREQUENCY) as Frequency[]).map((f) => (
              <option key={f} value={f}>
                {FREQUENCY[f]}
              </option>
            ))}
          </select>
        )}
      </Field>
      <div className="field-row">
        <Field label="First payment">
          {(control) => (
            <input
              {...control}
              type="date"
              min={today}
              value={form.start}
              onChange={(e) => {
                set("start")(e.target.value);
              }}
              required
            />
          )}
        </Field>
        <Field label="Last date (optional)">
          {(control) => (
            <input
              {...control}
              type="date"
              min={form.start || today}
              value={form.end}
              onChange={(e) => {
                set("end")(e.target.value);
              }}
            />
          )}
        </Field>
      </div>
      <Field label="Reference (optional)">
        {(control) => (
          <input
            {...control}
            maxLength={140}
            value={form.reference}
            onChange={(e) => {
              set("reference")(e.target.value);
            }}
          />
        )}
      </Field>
      {message && <Alert tone="error">{message}</Alert>}
      <p className="muted">
        Each payment is checked like any other transfer: limits, fraud checks and your balance on
        the day.
      </p>
      <div className="dialog__actions">
        <Button
          variant="secondary"
          onClick={() => {
            onDone(null);
          }}
        >
          Cancel
        </Button>
        <Button type="submit" loading={create.isPending}>
          Schedule transfer
        </Button>
      </div>
    </form>
  );
}

function OrderRow({
  order,
  toLabel,
  onCancel,
}: {
  order: ScheduledTransferOut;
  toLabel: string;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () =>
      order.status === "ACTIVE"
        ? api.pauseScheduledTransfer(order.id)
        : api.resumeScheduledTransfer(order.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
  const note = statusNote(order);
  const live = order.status === "ACTIVE" || order.status === "PAUSED";
  return (
    <tr>
      <td>
        {toLabel}
        {order.reference && <span className="muted"> · {order.reference}</span>}
        {note && <p className="row-note">{note}</p>}
        {toggle.isError && (
          <Alert tone="error">{errorMessage(toggle.error, "That did not work. Try again.")}</Alert>
        )}
      </td>
      <td className="right">{formatMinor(order.amount_minor, order.currency)}</td>
      <td>{FREQUENCY[order.frequency]}</td>
      <td>{live ? formatDate(order.next_run_date) : "—"}</td>
      <td>
        <StatusBadge status={STATUS[order.status]} />
      </td>
      <td className="row-actions">
        {live && order.pause_reason !== "FRAUD_BLOCK" && (
          <Button
            size="sm"
            variant="secondary"
            loading={toggle.isPending}
            onClick={() => {
              toggle.mutate();
            }}
          >
            {order.status === "ACTIVE" ? "Pause" : "Resume"}
          </Button>
        )}
        {live && (
          <Button size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </td>
    </tr>
  );
}

/** TASK-021: standing orders on the TASK-015 engine: upcoming list, schedule, pause, cancel. */
export function ScheduledTransfers({ now }: { now?: number }) {
  const queryClient = useQueryClient();
  const [openedAt] = useState(() => now ?? Date.now());
  const today = istDate(openedAt);
  const orders = useQuery({ queryKey: KEY, queryFn: api.scheduledTransfers });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const payees = useQuery({ queryKey: ["payees"], queryFn: api.payees });
  const [adding, setAdding] = useState(false);
  const [cancelling, setCancelling] = useState<ScheduledTransferOut | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Optimistic: the order leaves the upcoming list at once; it comes back if the API refuses.
  const cancel = useMutation({
    mutationFn: (order: ScheduledTransferOut) => api.cancelScheduledTransfer(order.id),
    onMutate: async (order) => {
      setCancelling(null);
      await queryClient.cancelQueries({ queryKey: KEY });
      const previous = queryClient.getQueryData<ScheduledTransferOut[]>(KEY);
      queryClient.setQueryData<ScheduledTransferOut[]>(KEY, (list) =>
        list?.map((o) => (o.id === order.id ? { ...o, status: "CANCELLED" } : o)),
      );
      return { previous };
    },
    onError: (_error, _order, context) => {
      if (context?.previous) queryClient.setQueryData(KEY, context.previous);
    },
    onSuccess: () => {
      setNotice("Scheduled transfer cancelled. No further payments will be made.");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });

  const activeAccounts = (accounts.data ?? []).filter((a) => a.status === "ACTIVE");
  const verified = (payees.data ?? []).filter((p) => p.status === "VERIFIED");
  const payeeName = new Map((payees.data ?? []).map((p) => [p.id, p.name]));
  const accountName = new Map(
    (accounts.data ?? []).map((a) => [
      a.id,
      `${accountTypeLabel(a.account_type)} ${a.account_number_masked}`,
    ]),
  );
  const toLabel = (o: ScheduledTransferOut) =>
    o.payee_id
      ? (payeeName.get(o.payee_id) ?? "A payee")
      : (accountName.get(o.to_account_id ?? "") ?? "Your account");
  const upcoming = (orders.data ?? []).filter(
    (o) => o.status === "ACTIVE" || o.status === "PAUSED",
  );
  const past = (orders.data ?? []).filter(
    (o) => o.status === "CANCELLED" || o.status === "COMPLETED",
  );

  return (
    <div className="page">
      <PageHeader
        title="Scheduled transfers"
        description="Standing orders that pay someone automatically on a schedule."
        actions={
          activeAccounts.length > 0 ? (
            <Button
              onClick={() => {
                setNotice(null);
                setAdding(true);
              }}
            >
              Schedule a transfer
            </Button>
          ) : undefined
        }
      />
      {notice && <Alert tone="success">{notice}</Alert>}
      {cancel.isError && (
        <Alert tone="error">
          {errorMessage(
            cancel.error,
            "The transfer could not be cancelled. It is still scheduled.",
          )}
        </Alert>
      )}
      {orders.isPending && <LoadingState label="Loading scheduled transfers..." />}
      {orders.isError && (
        <ErrorState
          message={errorMessage(orders.error, "Could not load your scheduled transfers.")}
          onRetry={() => void orders.refetch()}
        />
      )}
      {orders.data && upcoming.length === 0 && (
        <EmptyState message="No upcoming scheduled transfers." />
      )}
      {upcoming.length > 0 && (
        <div className="entries">
          <table>
            <caption className="sr-only">Upcoming scheduled transfers</caption>
            <thead>
              <tr>
                <th scope="col">To</th>
                <th scope="col" className="right">
                  Amount
                </th>
                <th scope="col">How often</th>
                <th scope="col">Next payment</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {upcoming.map((o) => (
                <OrderRow
                  key={o.id}
                  order={o}
                  toLabel={toLabel(o)}
                  onCancel={() => {
                    setCancelling(o);
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {past.length > 0 && (
        <details className="history">
          <summary>Cancelled and finished ({past.length})</summary>
          <ul className="list">
            {past.map((o) => (
              <li key={o.id}>
                {toLabel(o)} · {formatMinor(o.amount_minor, o.currency)} ·{" "}
                {FREQUENCY[o.frequency].toLowerCase()} · {STATUS[o.status].label.toLowerCase()}
              </li>
            ))}
          </ul>
        </details>
      )}
      <Dialog
        open={adding}
        title="Schedule a transfer"
        onClose={() => {
          setAdding(false);
        }}
      >
        {adding && (
          <NewSchedule
            accounts={activeAccounts}
            payees={verified}
            today={today}
            onDone={(created) => {
              setAdding(false);
              if (created) {
                setNotice(
                  `Scheduled. The first payment is on ${formatDate(created.next_run_date)}.`,
                );
              }
            }}
          />
        )}
      </Dialog>
      <ConfirmDialog
        open={cancelling !== null}
        title="Cancel this scheduled transfer?"
        items={
          cancelling
            ? [
                { label: "To", value: toLabel(cancelling) },
                {
                  label: "Amount",
                  value: `${formatMinor(cancelling.amount_minor, cancelling.currency)}, ${FREQUENCY[cancelling.frequency].toLowerCase()}`,
                },
              ]
            : []
        }
        confirmLabel="Cancel transfer"
        tone="danger"
        onConfirm={() => {
          if (cancelling) cancel.mutate(cancelling);
        }}
        onCancel={() => {
          setCancelling(null);
        }}
      />
    </div>
  );
}
