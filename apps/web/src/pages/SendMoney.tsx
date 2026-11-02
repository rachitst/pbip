import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, api } from "../api/client";
import type { AccountOut, PayeeOut, TransferOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";

interface Draft {
  from: AccountOut;
  payee: PayeeOut;
  amountMinor: number;
  reference: string;
}

/** What the customer sees for each final state the API can return. Reasons and scores are
 * never sent to customers, so none are shown. */
function Outcome({ transfer, payee }: { transfer: TransferOut; payee: PayeeOut | null }) {
  const amount = formatMinor(transfer.amount_minor, transfer.currency);
  const to = payee ? ` to ${payee.name} (${payee.account_number_masked})` : "";
  if (transfer.status === "POSTED") {
    return (
      <Alert tone="success">
        Sent {amount}
        {to}. Reference {transfer.id.slice(0, 8).toUpperCase()}. Your balance is updated above.
      </Alert>
    );
  }
  if (transfer.status === "PENDING_REVIEW") {
    return (
      <Alert tone="warning">
        Your transfer of {amount}
        {to} is being reviewed for your protection. The money is on hold, not sent, and we will
        notify you of the outcome.
      </Alert>
    );
  }
  if (transfer.status === "DECLINED") {
    return (
      <Alert tone="error">
        We could not process your transfer of {amount}. No money left your account. Contact support
        if you need help.
      </Alert>
    );
  }
  return <Alert tone="info">{transfer.message}</Alert>;
}

interface StepUpProps {
  transfer: TransferOut;
  onDone: (transfer: TransferOut) => void;
  onCancel: () => void;
}

/** TASK-037: TOTP prompt for a PENDING_STEP_UP transfer. The server counts the wrong codes and
 * escalates the transfer to review on the third; this dialog only shows what came back. */
function StepUpDialog({ transfer, onDone, onCancel }: StepUpProps) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the 6-digit code from your authenticator app.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onDone(await api.stepUp(transfer.id, code));
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422) {
        setError("That code did not work. Check your authenticator app and try again.");
        setCode("");
      } else if (caught instanceof ApiError && caught.status === 409) {
        onDone({ ...transfer, message: "This transfer is no longer waiting for a code." });
      } else {
        setError(errorMessage(caught, "The code could not be checked. Try again."));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open title="Confirm it's you" onClose={busy ? () => undefined : onCancel}>
      <p>
        For your protection, enter the 6-digit code from your authenticator app to send{" "}
        {formatMinor(transfer.amount_minor, transfer.currency)}.
      </p>
      <form onSubmit={(event) => void submit(event)}>
        <Field label="Authenticator code" error={error}>
          {(control) => (
            <input
              {...control}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              onChange={(event) => {
                setCode(event.target.value.replace(/\D/g, ""));
              }}
            />
          )}
        </Field>
        <div className="dialog__actions">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
            Not now
          </Button>
          <Button type="submit" loading={busy}>
            Confirm transfer
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

interface SendMoneyProps {
  accounts: AccountOut[];
  payees: PayeeOut[];
}

/** Send money: choose the source account and payee, review, confirm, then complete any step-up.
 * TASK-037. Every state shown comes from the API's response. */
export function SendMoney({ accounts, payees }: SendMoneyProps) {
  const queryClient = useQueryClient();
  const [fromId, setFromId] = useState(accounts[0]?.id ?? "");
  const [payeeId, setPayeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [reference, setReference] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [pending, setPending] = useState<TransferOut | null>(null);
  const [outcome, setOutcome] = useState<TransferOut | null>(null);
  // ONE key per user intent: a retry of the same confirmed draft reuses it, so a timeout can
  // never create a duplicate transfer.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    await queryClient.invalidateQueries({ queryKey: ["entries"] });
  };

  function review(event: SyntheticEvent) {
    event.preventDefault();
    setFormError(null);
    setOutcome(null);
    const from = accounts.find((a) => a.id === fromId);
    const payee = payees.find((p) => p.id === payeeId);
    const minor = parseToMinor(amount);
    if (!from || !payee || minor === null) {
      setFormError("Choose an account and a payee, and enter a valid amount (up to 2 decimals).");
      return;
    }
    if (minor > from.available_minor) {
      setFormError(
        `That is more than the ${formatMinor(from.available_minor, from.currency)} available.`,
      );
      return;
    }
    setDraft({ from, payee, amountMinor: minor, reference: reference.trim() });
  }

  async function confirm() {
    if (!draft) return;
    setSending(true);
    setSendError(null);
    try {
      const transfer = await api.transfer(
        {
          from_account_id: draft.from.id,
          payee_id: draft.payee.id,
          amount_minor: draft.amountMinor,
          reference: draft.reference === "" ? null : draft.reference,
        },
        intentKey,
      );
      setIntentKey(crypto.randomUUID()); // this intent is finished
      setDraft(null);
      if (transfer.status === "PENDING_STEP_UP") setPending(transfer);
      else setOutcome(transfer);
      await refresh();
    } catch (caught) {
      setSendError(errorMessage(caught, "The transfer could not be sent. Try again."));
    } finally {
      setSending(false);
    }
  }

  const chosenPayee = payees.find((p) => p.id === (outcome?.payee_id ?? payeeId)) ?? null;
  return (
    <section>
      <h2>Send money</h2>
      {payees.length === 0 ? (
        <p className="muted">
          Add and verify a payee first; you can send money once one is verified.
        </p>
      ) : (
        <form onSubmit={review}>
          <Field label="From account">
            {(control) => (
              <select
                {...control}
                value={fromId}
                onChange={(event) => {
                  setFromId(event.target.value);
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
          <Field label="Payee">
            {(control) => (
              <select
                {...control}
                value={payeeId}
                onChange={(event) => {
                  setPayeeId(event.target.value);
                }}
              >
                <option value="">Select a verified payee</option>
                {payees.map((payee) => (
                  <option key={payee.id} value={payee.id}>
                    {payee.name} ({payee.account_number_masked})
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Amount (INR)">
            {(control) => (
              <MoneyInput {...control} value={amount} onValueChange={setAmount} currency="INR" />
            )}
          </Field>
          <Field label="Reference (optional)">
            {(control) => (
              <input
                {...control}
                maxLength={140}
                value={reference}
                onChange={(event) => {
                  setReference(event.target.value);
                }}
              />
            )}
          </Field>
          {formError && <Alert tone="error">{formError}</Alert>}
          <Button type="submit">Review transfer</Button>
        </form>
      )}
      {outcome && <Outcome transfer={outcome} payee={chosenPayee} />}
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
                {
                  label: "To",
                  value: `${draft.payee.name} (${draft.payee.account_number_masked})`,
                },
                { label: "Amount", value: formatMinor(draft.amountMinor, draft.from.currency) },
                { label: "Reference", value: draft.reference || "—" },
              ]
            : []
        }
        confirmLabel="Send money"
        busy={sending}
        error={sendError}
        onConfirm={() => void confirm()}
        onCancel={() => {
          setDraft(null);
          setSendError(null);
        }}
      />
      {pending && (
        <StepUpDialog
          transfer={pending}
          onCancel={() => {
            setOutcome({
              ...pending,
              message:
                "Your transfer is waiting for your authenticator code. It has not been sent.",
            });
            setPending(null);
          }}
          onDone={(transfer) => {
            setPending(null);
            setOutcome(transfer);
            void refresh();
          }}
        />
      )}
    </section>
  );
}
