import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api/client";
import type { AccountOut, InstrumentOut, OrderOut, SuitabilityOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import {
  MICRO_PER_UNIT,
  SIMULATED_PRICE_NOTE,
  formatUnits,
  parseUnits,
  unitsForAmount,
} from "../lib/investing";
import { formatMinor, parseToMinor } from "../lib/money";
import { PRODUCT_PARAM } from "../lib/recommendations";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { StartSip } from "./StartSip";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

const KIND_LABEL: Record<InstrumentOut["kind"], string> = {
  MUTUAL_FUND: "Mutual fund",
  STOCK: "Stock",
  BOND: "Bond",
};

function accountLabel(account: AccountOut): string {
  return `${accountTypeLabel(account.account_type)} ${account.account_number_masked}`;
}

/** `?product=<id>` names an instrument, or a fund's catalogue row (`wealth_product_id`). */
export function preselected(instruments: InstrumentOut[], productId: string | null): InstrumentOut {
  const match = instruments.find((i) => i.id === productId || i.wealth_product_id === productId);
  const first = instruments[0];
  if (match) return match;
  if (first === undefined) throw new Error("no instruments");
  return first;
}

/** The customer's band for this instrument, as the server rated it (ADR-0058 section 9). */
function SuitabilityNote({
  suitability,
  instrument,
}: {
  suitability: SuitabilityOut | undefined;
  instrument: InstrumentOut;
}) {
  if (suitability === undefined) return null;
  const item = suitability.items.find((i) => i.product_id === instrument.id);
  if (item === undefined) return null;
  if (item.suitable === false) return <Alert tone="warning">{item.reason}</Alert>;
  return <p className="muted">{item.reason}</p>;
}

interface Draft {
  instrument: InstrumentOut;
  accountId: string;
  amountMinor: number | null;
  unitsMicro: number | null;
}

function orderItems(draft: Draft, account: AccountOut | undefined) {
  const { instrument } = draft;
  const items = [
    { label: "Investment", value: instrument.name },
    {
      label: "Today's simulated price",
      value: `${formatMinor(instrument.price_minor, "INR")} a unit`,
    },
    { label: "Pay from", value: account ? accountLabel(account) : "" },
  ];
  if (draft.amountMinor !== null) {
    items.push({ label: "Amount", value: formatMinor(draft.amountMinor, "INR") });
    const units = unitsForAmount(draft.amountMinor, instrument.price_minor);
    items.push({ label: "Units you get (about)", value: formatUnits(units) });
  }
  if (draft.unitsMicro !== null) {
    items.push({ label: "Units", value: formatUnits(draft.unitsMicro) });
    const cost = Math.trunc(draft.unitsMicro / MICRO_PER_UNIT) * instrument.price_minor;
    items.push({ label: "You pay", value: formatMinor(cost, "INR") });
  }
  return items;
}

function Receipt({ order }: { order: OrderOut }) {
  return (
    <section className="loan-card" aria-label="Order receipt">
      <h2>Order executed</h2>
      <dl className="details">
        <div className="details__row">
          <dt>Investment</dt>
          <dd>{order.instrument_name}</dd>
        </div>
        <div className="details__row">
          <dt>Units</dt>
          <dd>{formatUnits(order.units_micro)}</dd>
        </div>
        <div className="details__row">
          <dt>Debited</dt>
          <dd>{formatMinor(order.amount_minor, "INR")}</dd>
        </div>
        <div className="details__row">
          <dt>Reference</dt>
          <dd>{order.transaction_id}</dd>
        </div>
      </dl>
      <p>
        <Link to="/wealth">Back to your portfolio</Link>
      </p>
    </section>
  );
}

function OrderForm({
  instruments,
  accounts,
  suitability,
}: {
  instruments: InstrumentOut[];
  accounts: AccountOut[];
  suitability: SuitabilityOut | undefined;
}) {
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const [instrumentId, setInstrumentId] = useState(
    () => preselected(instruments, params.get(PRODUCT_PARAM)).id,
  );
  const instrument =
    instruments.find((i) => i.id === instrumentId) ?? preselected(instruments, null);
  const byAmount = instrument.order_mode === "AMOUNT";
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [amountText, setAmountText] = useState("");
  const [unitsText, setUnitsText] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [order, setOrder] = useState<OrderOut | null>(null);
  // ONE key per confirmed intent: retrying the same order reuses it (never debits twice).
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());

  const amountMinor = byAmount ? parseToMinor(amountText) : null;
  const unitsMicro = byAmount ? null : parseUnits(unitsText, true);
  const min = instrument.min_order_minor ?? 0;
  const amountError =
    byAmount && amountText !== "" && (amountMinor === null || amountMinor < min)
      ? `Enter at least ${formatMinor(min, "INR")}.`
      : null;
  const unitsError =
    !byAmount && unitsText !== "" && unitsMicro === null ? "Enter a whole number of units." : null;
  const valid = byAmount ? amountMinor !== null && amountMinor >= min : unitsMicro !== null;
  const draft: Draft = {
    instrument,
    accountId,
    amountMinor,
    unitsMicro,
  };

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const body =
        amountMinor !== null
          ? { instrument_id: instrument.id, account_id: accountId, amount_minor: amountMinor }
          : {
              instrument_id: instrument.id,
              account_id: accountId,
              units: Math.trunc((unitsMicro ?? 0) / MICRO_PER_UNIT),
            };
      setOrder(await api.buyInvestment(body, intentKey));
      await queryClient.invalidateQueries({ queryKey: ["wealth-holdings"] });
      await queryClient.invalidateQueries({ queryKey: ["wealth-portfolio"] });
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      setIntentKey(crypto.randomUUID());
      setReviewing(false);
    } catch (caught) {
      setError(errorMessage(caught, "The order could not be placed. Try again."));
    } finally {
      setBusy(false);
    }
  }

  if (order) return <Receipt order={order} />;
  return (
    <>
      <section className="loan-card" aria-label="Order">
        <Field label="Investment">
          {(control) => (
            <select
              {...control}
              value={instrument.id}
              onChange={(e) => {
                setInstrumentId(e.target.value);
                setIntentKey(crypto.randomUUID());
                setError(null);
              }}
            >
              {instruments.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name} · {KIND_LABEL[i.kind]}
                </option>
              ))}
            </select>
          )}
        </Field>
        <p>{instrument.description}</p>
        <dl className="details">
          <div className="details__row">
            <dt>Today&apos;s simulated price</dt>
            <dd>{formatMinor(instrument.price_minor, "INR")} a unit</dd>
          </div>
          <div className="details__row">
            <dt>Risk</dt>
            <dd>{instrument.risk_level.toLowerCase()}</dd>
          </div>
        </dl>
        <p className="muted">
          {instrument.notes} {SIMULATED_PRICE_NOTE}
        </p>
        <SuitabilityNote suitability={suitability} instrument={instrument} />
        {byAmount ? (
          <Field label="Amount" hint={`At least ${formatMinor(min, "INR")}`} error={amountError}>
            {(control) => (
              <MoneyInput
                {...control}
                currency="INR"
                value={amountText}
                onValueChange={(value) => {
                  setAmountText(value);
                  setError(null);
                }}
              />
            )}
          </Field>
        ) : (
          <Field label="Units" hint="Whole units" error={unitsError}>
            {(control) => (
              <input
                {...control}
                inputMode="numeric"
                value={unitsText}
                onChange={(e) => {
                  setUnitsText(e.target.value);
                  setError(null);
                }}
              />
            )}
          </Field>
        )}
        <Field label="Pay from">
          {(control) => (
            <select
              {...control}
              value={accountId}
              onChange={(e) => {
                setAccountId(e.target.value);
              }}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {accountLabel(a)} · {formatMinor(a.available_minor, a.currency)} available
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
          Review order
        </Button>
        <ConfirmDialog
          open={reviewing}
          title="Confirm your order"
          items={orderItems(
            draft,
            accounts.find((a) => a.id === accountId),
          )}
          confirmLabel="Buy now"
          busy={busy}
          error={error}
          onConfirm={() => void submit()}
          onCancel={() => {
            setReviewing(false);
            setError(null);
          }}
        />
      </section>
      {byAmount && <StartSip key={instrument.id} fund={instrument} accounts={accounts} />}
    </>
  );
}

/** ADR-0058: buy funds by amount (once, or monthly as a SIP) and whole stocks or bonds, at the
 * day's SIMULATED price, through the ledger. `?product=<id>` preselects an instrument. */
export default function Invest() {
  const instruments = useQuery({ queryKey: ["wealth-instruments"], queryFn: api.instruments });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const suitability = useQuery({ queryKey: ["wealth-suitability"], queryFn: api.suitability });
  const failed = instruments.isError ? instruments.error : accounts.isError ? accounts.error : null;
  const usable = (accounts.data ?? []).filter((a) => a.status === "ACTIVE" && a.currency === "INR");
  return (
    <div className="page">
      <PageHeader
        title="Invest"
        description="Buy funds, stocks and bonds at today's simulated price."
      />
      {failed !== null && (
        <ErrorState
          message={errorMessage(failed, "Could not load the investments or your accounts.")}
          onRetry={() => {
            void instruments.refetch();
            void accounts.refetch();
          }}
        />
      )}
      {failed === null && (instruments.isPending || accounts.isPending) && (
        <LoadingState label="Loading investments..." />
      )}
      {instruments.data?.length === 0 && <EmptyState message="Nothing can be bought right now." />}
      {accounts.data && usable.length === 0 && (
        <EmptyState message="Open an active INR account first to invest." />
      )}
      {instruments.data && instruments.data.length > 0 && usable.length > 0 && (
        <OrderForm
          instruments={instruments.data}
          accounts={usable}
          suitability={suitability.data}
        />
      )}
    </div>
  );
}
