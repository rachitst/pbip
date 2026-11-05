import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { AccountOut, InstrumentOut, SipOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel, formatDate } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";

const DAYS = Array.from({ length: 28 }, (_, i) => i + 1);

function accountLabel(account: AccountOut): string {
  return `${accountTypeLabel(account.account_type)} ${account.account_number_masked}`;
}

/** ADR-0058 section 7: set up a monthly purchase of one fund. Creating it moves no money; each
 * instalment is bought on its day at that day's simulated NAV by the hourly job. */
export function StartSip({ fund, accounts }: { fund: InstrumentOut; accounts: AccountOut[] }) {
  const queryClient = useQueryClient();
  const [amountText, setAmountText] = useState("");
  const [day, setDay] = useState(1);
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<SipOut | null>(null);
  // ONE key per confirmed intent: a retried submit never sets up a second SIP.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());
  const min = fund.min_order_minor ?? 0;
  const amount = parseToMinor(amountText);
  const valid = amount !== null && amount >= min;
  const amountError =
    amountText !== "" && !valid ? `Enter at least ${formatMinor(min, "INR")} a month.` : null;

  function changed() {
    setIntentKey(crypto.randomUUID());
    setError(null);
  }

  async function confirm() {
    if (amount === null) return;
    setBusy(true);
    setError(null);
    try {
      const sip = await api.createSip(
        { instrument_id: fund.id, account_id: accountId, amount_minor: amount, day_of_month: day },
        intentKey,
      );
      setCreated(sip);
      setReviewing(false);
      setIntentKey(crypto.randomUUID());
      await queryClient.invalidateQueries({ queryKey: ["wealth-sips"] });
    } catch (caught) {
      setError(errorMessage(caught, "The SIP could not be set up. Try again."));
    } finally {
      setBusy(false);
    }
  }

  const account = accounts.find((a) => a.id === accountId);
  return (
    <section className="loan-card" aria-labelledby="start-sip-title">
      <h2 id="start-sip-title">Start a monthly SIP</h2>
      {created ? (
        <p role="status">
          SIP set up: {formatMinor(created.amount_minor, "INR")} into {created.instrument_name}{" "}
          every month on day {created.day_of_month}. The first instalment is on{" "}
          {created.next_run_on ? formatDate(created.next_run_on) : "its day"}; nothing is debited
          today.
        </p>
      ) : (
        <>
          <p className="muted">
            {fund.name} is bought every month on the day you choose, at that day&apos;s simulated
            price. Nothing is debited today.
          </p>
          <Field
            label="Monthly amount"
            hint={`At least ${formatMinor(min, "INR")}`}
            error={amountError}
          >
            {(control) => (
              <MoneyInput
                {...control}
                currency="INR"
                value={amountText}
                onValueChange={(value) => {
                  setAmountText(value);
                  changed();
                }}
              />
            )}
          </Field>
          <Field label="Day of the month" hint="1 to 28">
            {(control) => (
              <select
                {...control}
                value={day}
                onChange={(e) => {
                  setDay(Number.parseInt(e.target.value, 10));
                  changed();
                }}
              >
                {DAYS.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Pay every month from">
            {(control) => (
              <select
                {...control}
                value={accountId}
                onChange={(e) => {
                  setAccountId(e.target.value);
                  changed();
                }}
              >
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {accountLabel(a)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Button
            disabled={!valid || accountId === ""}
            onClick={() => {
              setError(null);
              setReviewing(true);
            }}
          >
            Review SIP
          </Button>
          <ConfirmDialog
            open={reviewing}
            title="Confirm your monthly SIP"
            items={[
              { label: "Fund", value: fund.name },
              { label: "Every month", value: amount === null ? "" : formatMinor(amount, "INR") },
              { label: "On day", value: String(day) },
              { label: "From", value: account ? accountLabel(account) : "" },
            ]}
            confirmLabel="Start SIP"
            busy={busy}
            error={error}
            onConfirm={() => void confirm()}
            onCancel={() => {
              setReviewing(false);
              setError(null);
            }}
          />
        </>
      )}
    </section>
  );
}
