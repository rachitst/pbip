import { useQuery } from "@tanstack/react-query";
import { Landmark } from "lucide-react";
import { api } from "../api/client";
import type { CurrencyTotalOut } from "../api/client";
import { formatMinor } from "../lib/money";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import "./Accounts.css";

/** FR-08 total balance overview (TASK-109). The server adds up the customer's own savings and
 * current accounts, one row per currency: amounts in different currencies are never summed here.
 * The ["accounts", ...] key is refreshed whenever the accounts list is (opening, transfers). */
export function TotalBalance() {
  const summary = useQuery({ queryKey: ["accounts", "summary"], queryFn: api.accountsSummary });

  return (
    <section className="total-balance" aria-labelledby="total-balance-title">
      <div className="total-balance__head">
        <span className="total-balance__icon">
          <Landmark size={18} aria-hidden="true" focusable="false" />
        </span>
        <h2 id="total-balance-title" className="total-balance__title">
          Total balance
        </h2>
      </div>
      {summary.isPending && <LoadingState label="Loading total balance..." />}
      {summary.isError && (
        <ErrorState
          message="Could not load your total balance."
          onRetry={() => {
            void summary.refetch();
          }}
        />
      )}
      {summary.data?.totals.length === 0 && (
        <EmptyState message="No open accounts, so there is no balance to total yet." />
      )}
      {summary.data?.totals.map((total) => (
        <CurrencyTotal key={total.currency} total={total} />
      ))}
      {summary.data && summary.data.totals.length > 0 && (
        <p className="total-balance__note">
          Savings and current accounts you hold, including joint accounts. Available excludes
          amounts on hold and frozen accounts; closed accounts are not counted.
        </p>
      )}
    </section>
  );
}

function CurrencyTotal({ total }: { total: CurrencyTotalOut }) {
  const accounts = `${String(total.account_count)} account${total.account_count === 1 ? "" : "s"}`;
  return (
    <div className="total-balance__row" data-testid={`total-${total.currency}`}>
      <div className="total-balance__main">
        <div aria-label={`total available ${total.currency}`} className="total-balance__value">
          {formatMinor(total.available_minor, total.currency)}
        </div>
        <div className="total-balance__sub">Available across {accounts}</div>
      </div>
      <div className="total-balance__tiles">
        <div aria-label={`total ledger ${total.currency}`} className="total-balance__tile">
          <span className="total-balance__tile-label">
            Ledger<span className="sr-only">:</span>
          </span>{" "}
          <span className="total-balance__tile-value">
            {formatMinor(total.ledger_balance_minor, total.currency)}
          </span>
        </div>
        <div aria-label={`total held ${total.currency}`} className="total-balance__tile">
          <span className="total-balance__tile-label">
            On hold<span className="sr-only">:</span>
          </span>{" "}
          <span className="total-balance__tile-value">
            {formatMinor(total.held_minor, total.currency)}
          </span>
        </div>
        {total.frozen_account_count > 0 && (
          <div className="total-balance__tile total-balance__tile--note">
            {String(total.frozen_account_count)} frozen, not counted as available
          </div>
        )}
      </div>
    </div>
  );
}
