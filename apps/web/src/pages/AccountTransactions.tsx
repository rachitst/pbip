import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EntriesPage } from "../api/client";
import { formatMinor } from "../lib/money";

const PAGE_SIZE = 10;

const dateFormat = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" });

interface Props {
  accountId: string;
  currency: string;
}

/** Ledger entries, newest first. "Load older" follows the server's keyset cursor, so new
 * postings never shift or duplicate rows already on screen (unlike offset pagination). */
export function AccountTransactions({ accountId, currency }: Props) {
  const entries = useInfiniteQuery({
    queryKey: ["entries", accountId],
    queryFn: ({ pageParam }) => api.accountEntries(accountId, pageParam, PAGE_SIZE),
    initialPageParam: null as string | null,
    getNextPageParam: (last: EntriesPage) => (last.has_more ? last.next_cursor : null),
  });

  if (entries.isPending) return <p className="muted">Loading transactions...</p>;
  // Only a failed FIRST page replaces the list; a failed "Load older" keeps the rows on screen.
  if (entries.isLoadingError) {
    return (
      <p role="alert" className="error">
        Could not load transactions.
      </p>
    );
  }
  const items = entries.data.pages.flatMap((page) => page.items);
  if (items.length === 0) return <p className="muted">No transactions yet.</p>;

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
            <tr key={entry.entry_seq}>
              <td>{dateFormat.format(new Date(entry.created_at))}</td>
              <td>
                {entry.reference ?? entry.transaction_type}
                {entry.transaction_status !== "POSTED" && (
                  <span className="muted"> ({entry.transaction_status})</span>
                )}
              </td>
              <td className={entry.direction === "CREDIT" ? "right ok" : "right"}>
                {formatMinor(
                  entry.direction === "CREDIT" ? entry.amount_minor : -entry.amount_minor,
                  currency,
                )}
              </td>
              <td className="right">{formatMinor(entry.balance_after_minor, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {entries.hasNextPage && (
        <button
          type="button"
          className="secondary"
          disabled={entries.isFetchingNextPage}
          onClick={() => {
            void entries.fetchNextPage();
          }}
        >
          {entries.isFetchingNextPage ? "Loading..." : "Load older"}
        </button>
      )}
      {entries.isFetchNextPageError && (
        <p role="alert" className="error">
          Could not load older transactions.
        </p>
      )}
    </div>
  );
}
