import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EntriesPage } from "../api/client";
import { formatDateTime } from "../lib/format";
import { formatMinor } from "../lib/money";
import { transactionStatus } from "../lib/status";
import { Amount } from "../ui/Amount";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

const PAGE_SIZE = 10;

interface Props {
  accountId: string;
  currency: string;
  /** Set when the page was opened from a notification: that transaction's row is marked. */
  highlightTransactionId?: string | null;
}

/** Ledger entries, newest first. "Load older" follows the server's keyset cursor, so new
 * postings never shift or duplicate rows already on screen (unlike offset pagination). */
export function AccountTransactions({ accountId, currency, highlightTransactionId }: Props) {
  const entries = useInfiniteQuery({
    queryKey: ["entries", accountId],
    queryFn: ({ pageParam }) => api.accountEntries(accountId, pageParam, PAGE_SIZE),
    initialPageParam: null as string | null,
    getNextPageParam: (last: EntriesPage) => (last.has_more ? last.next_cursor : null),
  });

  if (entries.isPending) return <LoadingState label="Loading transactions..." />;
  // Only a failed FIRST page replaces the list; a failed "Load older" keeps the rows on screen.
  if (entries.isLoadingError) {
    return (
      <ErrorState
        message="Could not load transactions."
        onRetry={() => {
          void entries.refetch();
        }}
      />
    );
  }
  const items = entries.data.pages.flatMap((page) => page.items);
  if (items.length === 0) return <EmptyState message="No transactions yet." />;

  return (
    <div className="entries">
      <table>
        <caption className="sr-only">Transactions, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Details</th>
            <th scope="col" className="right">
              Amount
            </th>
            <th scope="col" className="right">
              Balance
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((entry) => (
            <tr
              key={entry.entry_seq}
              className={
                entry.transaction_id === highlightTransactionId ? "entry--highlight" : undefined
              }
              aria-current={entry.transaction_id === highlightTransactionId ? "true" : undefined}
            >
              <td>{formatDateTime(entry.created_at)}</td>
              <td>
                {entry.reference ?? entry.transaction_type}
                {entry.transaction_status !== "POSTED" && (
                  <>
                    {" "}
                    <StatusBadge status={transactionStatus(entry.transaction_status)} />
                  </>
                )}
              </td>
              <td className="right">
                {entry.direction === "CREDIT" ? (
                  <Amount minor={entry.amount_minor} currency={currency} credit />
                ) : (
                  <Amount minor={-entry.amount_minor} currency={currency} />
                )}
              </td>
              <td className="right">{formatMinor(entry.balance_after_minor, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {entries.hasNextPage && (
        <Button
          variant="secondary"
          loading={entries.isFetchingNextPage}
          onClick={() => {
            void entries.fetchNextPage();
          }}
        >
          {entries.isFetchingNextPage ? "Loading..." : "Load older"}
        </Button>
      )}
      {entries.isFetchNextPageError && (
        <Alert tone="error">Could not load older transactions.</Alert>
      )}
    </div>
  );
}
