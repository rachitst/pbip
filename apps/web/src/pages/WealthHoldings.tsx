import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { AccountOut, HoldingOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import {
  SIMULATED_PRICE_NOTE,
  formatGainBps,
  formatUnits,
  parseUnits,
  valueOfUnits,
} from "../lib/investing";
import { formatMinor } from "../lib/money";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

const KIND: Record<HoldingOut["kind"], string> = {
  MUTUAL_FUND: "Fund",
  STOCK: "Stock",
  BOND: "Bond",
};

function SellHolding({ holding, accounts }: { holding: HoldingOut; accounts: AccountOut[] }) {
  const queryClient = useQueryClient();
  const wholeOnly = holding.kind !== "MUTUAL_FUND";
  const [open, setOpen] = useState(false);
  const [unitsText, setUnitsText] = useState("");
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // ONE key per confirmed sale: retrying the same sale never credits twice.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());
  const units = parseUnits(unitsText, wholeOnly);
  const tooMany = units !== null && units > holding.units_micro;
  const proceeds = units === null ? null : valueOfUnits(units, holding.price_minor);

  async function confirm() {
    if (units === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.sellHolding(holding.id, { account_id: accountId, units_micro: units }, intentKey);
      setIntentKey(crypto.randomUUID());
      setOpen(false);
      setUnitsText("");
      await queryClient.invalidateQueries({ queryKey: ["wealth-holdings"] });
      await queryClient.invalidateQueries({ queryKey: ["wealth-portfolio"] });
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    } catch (caught) {
      setError(errorMessage(caught, "The sale could not be made. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Field
        label={`Units of ${holding.instrument_name} to sell`}
        hint={`You hold ${formatUnits(holding.units_micro)}`}
        error={
          unitsText !== "" && (units === null || tooMany)
            ? wholeOnly
              ? "Enter a whole number of units you hold."
              : "Enter up to six decimals, no more than you hold."
            : null
        }
      >
        {(control) => (
          <input
            {...control}
            inputMode="decimal"
            value={unitsText}
            onChange={(e) => {
              setUnitsText(e.target.value);
              setIntentKey(crypto.randomUUID());
            }}
          />
        )}
      </Field>
      <Button
        size="sm"
        variant="secondary"
        disabled={units === null || tooMany || accountId === ""}
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
      >
        Sell
      </Button>
      <ConfirmDialog
        open={open}
        title={`Sell ${holding.instrument_name}`}
        items={[
          { label: "Units", value: units === null ? "" : formatUnits(units) },
          {
            label: "Today's simulated price",
            value: `${formatMinor(holding.price_minor, "INR")} a unit`,
          },
          { label: "You receive", value: proceeds === null ? "" : formatMinor(proceeds, "INR") },
          {
            label: "Into",
            value: (() => {
              const a = accounts.find((x) => x.id === accountId);
              return a ? `${accountTypeLabel(a.account_type)} ${a.account_number_masked}` : "";
            })(),
          },
        ]}
        confirmLabel="Sell now"
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        onCancel={() => {
          setOpen(false);
          setError(null);
        }}
      />
      {accounts.length > 1 && (
        <Field label="Receive into">
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
                  {accountTypeLabel(a.account_type)} {a.account_number_masked}
                </option>
              ))}
            </select>
          )}
        </Field>
      )}
    </>
  );
}

/** ADR-0058: fund, stock and bond holdings at today's SIMULATED price with gain or loss. */
export function HoldingsSection() {
  const holdings = useQuery({ queryKey: ["wealth-holdings"], queryFn: api.holdings });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const usable = (accounts.data ?? []).filter((a) => a.status === "ACTIVE" && a.currency === "INR");
  if (holdings.isPending) return <LoadingState label="Loading your investments..." />;
  if (holdings.isError) {
    return (
      <ErrorState
        message={errorMessage(holdings.error, "Could not load your investments.")}
        onRetry={() => {
          void holdings.refetch();
        }}
      />
    );
  }
  if (holdings.data.length === 0) {
    return <EmptyState message="You hold no funds, stocks or bonds yet." />;
  }
  return (
    <div className="entries">
      <p className="muted">{SIMULATED_PRICE_NOTE}</p>
      <table>
        <caption className="sr-only">My fund, stock and bond holdings</caption>
        <thead>
          <tr>
            <th scope="col">Investment</th>
            <th scope="col" className="right">
              Units
            </th>
            <th scope="col" className="right">
              Value today
            </th>
            <th scope="col" className="right">
              Cost
            </th>
            <th scope="col" className="right">
              Gain or loss
            </th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {holdings.data.map((h) => (
            <tr key={h.id}>
              <td>
                {h.instrument_name}
                <br />
                <span className="muted">{KIND[h.kind]}</span>
              </td>
              <td className="right">{formatUnits(h.units_micro)}</td>
              <td className="right">{formatMinor(h.value_minor, "INR")}</td>
              <td className="right">{formatMinor(h.cost_basis_minor, "INR")}</td>
              <td className="right">
                {formatMinor(h.gain_minor, "INR")}
                {h.gain_bps !== null && (
                  <>
                    <br />
                    <span className="muted">{formatGainBps(h.gain_bps)}</span>
                  </>
                )}
              </td>
              <td>{usable.length > 0 && <SellHolding holding={h} accounts={usable} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
