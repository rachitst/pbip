import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { AccountOut, BillPaymentOut, BillerOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel, formatDateTime } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import type { Tone } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

const CATEGORY: Record<string, string> = {
  ELECTRICITY: "Electricity",
  WATER: "Water",
  TELECOM: "Mobile and telephone",
  GAS: "Gas",
  BROADBAND: "Broadband",
  INSURANCE: "Insurance",
};

const STATUS: Record<string, { label: string; tone: Tone }> = {
  POSTED: { label: "Paid", tone: "success" },
  PENDING_REVIEW: { label: "On hold for review", tone: "warning" },
  DECLINED: { label: "Not paid", tone: "danger" },
  REVERSED: { label: "Reversed", tone: "neutral" },
};

interface Draft {
  from: AccountOut;
  biller: BillerOut;
  consumerReference: string;
  amountMinor: number;
}

/** The receipt for a paid bill; held and declined payments say plainly that nothing was paid. */
export function Receipt({ payment }: { payment: BillPaymentOut }) {
  const amount = formatMinor(payment.amount_minor, payment.currency);
  if (payment.status === "PENDING_REVIEW") {
    return (
      <Alert tone="warning">
        Your payment of {amount} to {payment.biller_name} is being reviewed for your protection. The
        money is on hold, not paid, and we will notify you of the outcome.
      </Alert>
    );
  }
  if (payment.status !== "POSTED") {
    return (
      <Alert tone="error">
        We could not pay {payment.biller_name}. No money left your account. Contact support if you
        need help.
      </Alert>
    );
  }
  return (
    <section className="card receipt" aria-labelledby="receipt-title">
      <h2 id="receipt-title">Bill paid</h2>
      <dl className="details">
        <div className="details__row">
          <dt>Amount debited</dt>
          <dd>{amount}</dd>
        </div>
        <div className="details__row">
          <dt>Paid to</dt>
          <dd>
            {payment.biller_name} ({payment.biller_code})
          </dd>
        </div>
        <div className="details__row">
          <dt>Consumer number</dt>
          <dd>{payment.consumer_reference}</dd>
        </div>
        <div className="details__row">
          <dt>Clearing reference</dt>
          <dd>{payment.reference}</dd>
        </div>
        <div className="details__row">
          <dt>New balance</dt>
          <dd>{formatMinor(payment.new_balance_minor, payment.currency)}</dd>
        </div>
        <div className="details__row">
          <dt>Paid on</dt>
          <dd>{formatDateTime(payment.created_at)}</dd>
        </div>
      </dl>
    </section>
  );
}

function History() {
  const payments = useQuery({ queryKey: ["bill-payments"], queryFn: api.billPayments });
  if (payments.isPending) return <LoadingState label="Loading your bill payments..." />;
  if (payments.isError) {
    return (
      <ErrorState
        message={errorMessage(payments.error, "Could not load your bill payments.")}
        onRetry={() => {
          void payments.refetch();
        }}
      />
    );
  }
  if (payments.data.length === 0) return <EmptyState message="No bill payments yet." />;
  return (
    <div className="entries">
      <table>
        <caption className="sr-only">Your recent bill payments, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Biller</th>
            <th scope="col">Consumer number</th>
            <th scope="col" className="right">
              Amount
            </th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {payments.data.map((p) => (
            <tr key={p.id}>
              <td>{formatDateTime(p.created_at)}</td>
              <td>{p.biller_name}</td>
              <td>{p.consumer_reference}</td>
              <td className="right">{formatMinor(p.amount_minor, p.currency)}</td>
              <td>
                <StatusBadge status={STATUS[p.status] ?? { label: p.status, tone: "neutral" }} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PayForm({ accounts, billers }: { accounts: AccountOut[]; billers: BillerOut[] }) {
  const queryClient = useQueryClient();
  const [fromId, setFromId] = useState(accounts[0]?.id ?? "");
  const [billerId, setBillerId] = useState("");
  const [consumerReference, setConsumerReference] = useState("");
  const [amount, setAmount] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [paying, setPaying] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<BillPaymentOut | null>(null);
  // ONE key per confirmed intent: retrying the same draft reuses it, so a timeout never pays twice.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());

  function review(event: SyntheticEvent) {
    event.preventDefault();
    setFormError(null);
    setReceipt(null);
    const from = accounts.find((a) => a.id === fromId);
    const biller = billers.find((b) => b.id === billerId);
    const minor = parseToMinor(amount);
    const ref = consumerReference.trim();
    if (!from || !biller || minor === null || ref.length < 2) {
      setFormError(
        "Choose an account and a biller, enter your consumer number and a valid amount (up to 2 decimals).",
      );
      return;
    }
    if (minor > from.available_minor) {
      setFormError(
        `That is more than the ${formatMinor(from.available_minor, from.currency)} available.`,
      );
      return;
    }
    setDraft({ from, biller, consumerReference: ref, amountMinor: minor });
  }

  async function confirm() {
    if (!draft) return;
    setPaying(true);
    setPayError(null);
    try {
      const payment = await api.payBill(
        {
          from_account_id: draft.from.id,
          biller_id: draft.biller.id,
          consumer_reference: draft.consumerReference,
          amount_minor: draft.amountMinor,
        },
        intentKey,
      );
      setIntentKey(crypto.randomUUID());
      setDraft(null);
      setReceipt(payment);
      setAmount("");
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      await queryClient.invalidateQueries({ queryKey: ["bill-payments"] });
    } catch (caught) {
      setPayError(errorMessage(caught, "The bill could not be paid. Try again."));
    } finally {
      setPaying(false);
    }
  }

  return (
    <>
      <form onSubmit={review}>
        <Field label="From account">
          {(control) => (
            <select
              {...control}
              value={fromId}
              onChange={(e) => {
                setFromId(e.target.value);
              }}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {accountTypeLabel(a.account_type)} {a.account_number_masked} ·{" "}
                  {formatMinor(a.available_minor, a.currency)} available
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Biller">
          {(control) => (
            <select
              {...control}
              value={billerId}
              onChange={(e) => {
                setBillerId(e.target.value);
              }}
            >
              <option value="">Select a biller</option>
              {billers.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name} · {CATEGORY[b.category] ?? b.category}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Consumer number" hint="Printed on your bill.">
          {(control) => (
            <input
              {...control}
              value={consumerReference}
              maxLength={64}
              onChange={(e) => {
                setConsumerReference(e.target.value);
              }}
            />
          )}
        </Field>
        <Field label="Amount (INR)">
          {(control) => (
            <MoneyInput {...control} value={amount} onValueChange={setAmount} currency="INR" />
          )}
        </Field>
        {formError && <Alert tone="error">{formError}</Alert>}
        <Button type="submit">Review payment</Button>
      </form>
      {receipt && <Receipt payment={receipt} />}
      <ConfirmDialog
        open={draft !== null}
        title="Check and confirm"
        items={
          draft
            ? [
                {
                  label: "From",
                  value: `${accountTypeLabel(draft.from.account_type)} ${draft.from.account_number_masked}`,
                },
                { label: "Biller", value: draft.biller.name },
                { label: "Consumer number", value: draft.consumerReference },
                { label: "Amount", value: formatMinor(draft.amountMinor, draft.from.currency) },
              ]
            : []
        }
        confirmLabel="Pay bill"
        busy={paying}
        error={payError}
        onConfirm={() => void confirm()}
        onCancel={() => {
          setDraft(null);
          setPayError(null);
        }}
      />
    </>
  );
}

/** TASK-020: pay a utility bill from an own account, with a receipt and recent payments. */
export function PayBills() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const billers = useQuery({ queryKey: ["billers"], queryFn: api.billers });
  const failed = accounts.isError ? accounts.error : billers.isError ? billers.error : null;
  const usable = (accounts.data ?? []).filter((a) => a.status === "ACTIVE");
  return (
    <div className="page">
      <PageHeader
        title="Pay bills"
        description="Pay electricity, water, gas, phone and broadband bills."
      />
      {(accounts.isPending || billers.isPending) && !failed && (
        <LoadingState label="Loading your accounts and billers..." />
      )}
      {failed && (
        <ErrorState
          message={errorMessage(failed, "Could not load your accounts or the billers.")}
          onRetry={() => {
            void accounts.refetch();
            void billers.refetch();
          }}
        />
      )}
      {accounts.data && billers.data && usable.length === 0 && (
        <EmptyState message="Open an account first to pay bills from it." />
      )}
      {accounts.data && billers.data && usable.length > 0 && billers.data.length === 0 && (
        <EmptyState message="No billers are available right now." />
      )}
      {accounts.data && billers.data && usable.length > 0 && billers.data.length > 0 && (
        <section>
          <PayForm accounts={usable} billers={billers.data} />
        </section>
      )}
      <section>
        <h2>Recent bill payments</h2>
        <History />
      </section>
    </div>
  );
}
