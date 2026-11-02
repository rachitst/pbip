import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CurrencyTotalOut } from "../api/client";
import { formatMinor } from "../lib/money";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

/** FR-08 total balance overview (TASK-109). The server adds up the customer's own savings and
 * current accounts, one row per currency: amounts in different currencies are never summed here.
 * The ["accounts", ...] key is refreshed whenever the accounts list is (opening, transfers). */
export function TotalBalance() {
  const summary = useQuery({ queryKey: ["accounts", "summary"], queryFn: api.accountsSummary });

  return (
    <section className="total-balance" aria-labelledby="total-balance-title">
      <h2 id="total-balance-title" className="total-balance__title">
        Total balance
      </h2>
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
        <p className="muted">
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
      <div>
        <div aria-label={`total available ${total.currency}`} className="total-balance__value">
          {formatMinor(total.available_minor, total.currency)}
        </div>
        <div className="muted">Available across {accounts}</div>
      </div>
      <div className="right">
        <div aria-label={`total ledger ${total.currency}`} className="muted">
          Ledger: {formatMinor(total.ledger_balance_minor, total.currency)}
        </div>
        <div aria-label={`total held ${total.currency}`} className="muted">
          On hold: {formatMinor(total.held_minor, total.currency)}
        </div>
        {total.frozen_account_count > 0 && (
          <div className="muted">
            {String(total.frozen_account_count)} frozen, not counted as available
          </div>
        )}
      </div>
    </div>
  );
}
