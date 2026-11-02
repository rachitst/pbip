import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { EntriesPage, EntryFilters } from "../api/client";
import { formatDateTime } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import { transactionStatus } from "../lib/status";
import { Amount } from "../ui/Amount";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

const PAGE_SIZE = 10;

interface Props {
  accountId: string;
  currency: string;
  /** Set when the page was opened from a notification: that transaction's row is marked. */
  highlightTransactionId?: string | null;
}

interface Draft {
  search: string;
  type: "" | "DEBIT" | "CREDIT";
  start: string;
  end: string;
  min: string;
  max: string;
}

const EMPTY: Draft = { search: "", type: "", start: "", end: "", min: "", max: "" };

/** Turns the form into API filters, or a message for the first problem found. */
export function toFilters(d: Draft): EntryFilters | string {
  const min = d.min.trim() === "" ? undefined : parseToMinor(d.min);
  const max = d.max.trim() === "" ? undefined : parseToMinor(d.max);
  if (min === null || max === null) return "Enter amounts like 1500 or 1500.50.";
  if (min !== undefined && max !== undefined && min > max) {
    return "The minimum amount must not be more than the maximum.";
  }
  if (d.start && d.end && d.start > d.end)
    return "The start date must be on or before the end date.";
  const filters: EntryFilters = {};
  if (d.search.trim()) filters.search = d.search.trim();
  if (d.type) filters.type = d.type;
  if (d.start) filters.start_date = d.start;
  if (d.end) filters.end_date = d.end;
  if (min !== undefined) filters.min_amount = min;
  if (max !== undefined) filters.max_amount = max;
  return filters;
}

function FilterForm({ onApply }: { onApply: (filters: EntryFilters) => void }) {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [problem, setProblem] = useState<string | null>(null);
  const set = (key: keyof Draft) => (value: string) => {
    setDraft({ ...draft, [key]: value });
  };
  function submit(event: SyntheticEvent) {
    event.preventDefault();
    const result = toFilters(draft);
    if (typeof result === "string") {
      setProblem(result);
      return;
    }
    setProblem(null);
    onApply(result);
  }
  return (
    <form className="filters" onSubmit={submit} aria-label="Search transactions">
      <Field label="Search references">
        {(control) => (
          <input
            {...control}
            type="search"
            maxLength={64}
            value={draft.search}
            onChange={(e) => {
              set("search")(e.target.value);
            }}
          />
        )}
      </Field>
      <Field label="Money in or out">
        {(control) => (
          <select
            {...control}
            value={draft.type}
            onChange={(e) => {
              set("type")(e.target.value);
            }}
          >
            <option value="">Both</option>
            <option value="CREDIT">Money in</option>
            <option value="DEBIT">Money out</option>
          </select>
        )}
      </Field>
      <Field label="From date">
        {(control) => (
          <input
            {...control}
            type="date"
            value={draft.start}
            onChange={(e) => {
              set("start")(e.target.value);
            }}
          />
        )}
      </Field>
      <Field label="To date">
        {(control) => (
          <input
            {...control}
            type="date"
            value={draft.end}
            onChange={(e) => {
              set("end")(e.target.value);
            }}
          />
        )}
      </Field>
      <Field label="Minimum amount (INR)">
        {(control) => (
          <MoneyInput {...control} value={draft.min} onValueChange={set("min")} currency="INR" />
        )}
      </Field>
      <Field label="Maximum amount (INR)">
        {(control) => (
          <MoneyInput {...control} value={draft.max} onValueChange={set("max")} currency="INR" />
        )}
      </Field>
      {problem && <Alert tone="error">{problem}</Alert>}
      <div className="filters__actions">
        <Button type="submit" size="sm">
          Apply filters
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => {
            setDraft(EMPTY);
            setProblem(null);
            onApply({});
          }}
        >
          Clear
        </Button>
      </div>
    </form>
  );
}

/** Ledger entries, newest first. "Load older" follows the server's keyset cursor, so new
 * postings never shift or duplicate rows already on screen (unlike offset pagination). */
export function AccountTransactions({ accountId, currency, highlightTransactionId }: Props) {
  const [filters, setFilters] = useState<EntryFilters>({});
  const filtered = Object.keys(filters).length > 0;
  return (
    <>
      <FilterForm onApply={setFilters} />
      <EntryList
        accountId={accountId}
        currency={currency}
        highlightTransactionId={highlightTransactionId}
        filters={filters}
        filtered={filtered}
      />
    </>
  );
}

function EntryList({
  accountId,
  currency,
  highlightTransactionId,
  filters,
  filtered,
}: Props & { filters: EntryFilters; filtered: boolean }) {
  // The filters are part of the key: each filtered list has its own cursor chain.
  const entries = useInfiniteQuery({
    queryKey: ["entries", accountId, filters],
    queryFn: ({ pageParam }) => api.accountEntries(accountId, pageParam, PAGE_SIZE, filters),
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
  if (items.length === 0) {
    return (
      <EmptyState
        message={filtered ? "No transactions match these filters." : "No transactions yet."}
      />
    );
  }

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
