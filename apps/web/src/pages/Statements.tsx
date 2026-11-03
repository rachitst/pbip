import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import "./ChoiceGroup.css";

type Format = "pdf" | "csv";
const MAX_DAYS = 366;
const DAY_MS = 86_400_000;

/** YYYY-MM-DD of a date in India (IST, the bank's statement days), from an instant. */
export function istDate(at: number): string {
  return new Date(at + 5.5 * 3_600_000).toISOString().slice(0, 10);
}

/** First day of the current month to today: the period most customers want. */
export function defaultPeriod(now: number): { start: string; end: string } {
  const end = istDate(now);
  return { start: `${end.slice(0, 8)}01`, end };
}

export function periodProblem(start: string, end: string, today: string): string | null {
  if (!start || !end) return "Choose a start and an end date.";
  if (start > end) return "The start date must be on or before the end date.";
  if (end > today) return "The statement cannot end in the future.";
  const days = (Date.parse(end) - Date.parse(start)) / DAY_MS + 1;
  if (days > MAX_DAYS) return `A statement can cover at most ${MAX_DAYS} days.`;
  return null;
}

/** Hands a downloaded blob to the browser as a file. */
function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** TASK-074 / UC-03: choose an account and a period, download a PDF or CSV statement. */
export function Statements({ now }: { now?: number }) {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  // Read the clock once, when the page opens (tests pass a fixed instant).
  const [openedAt] = useState(() => now ?? Date.now());
  const [initial] = useState(() => defaultPeriod(openedAt));
  const [accountId, setAccountId] = useState("");
  const [start, setStart] = useState(initial.start);
  const [end, setEnd] = useState(initial.end);
  const [format, setFormat] = useState<Format>("pdf");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const today = istDate(openedAt);
  const chosen = accountId || accounts.data?.[0]?.id || "";

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setDone(null);
    const problem = periodProblem(start, end, today);
    setError(problem);
    if (problem || !chosen) return;
    setBusy(true);
    try {
      const file = await api.statement(chosen, start, end, format);
      const name = file.filename ?? `pbip-statement-${start}-to-${end}.${format}`;
      save(file.blob, name);
      setDone(`Downloaded ${name}.`);
    } catch (caught) {
      setError(errorMessage(caught, "The statement could not be downloaded. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <PageHeader
        title="Statements"
        description="Download a statement for any period of up to a year. Dates are India time."
      />
      {accounts.isPending && <LoadingState label="Loading your accounts..." />}
      {accounts.isError && (
        <ErrorState
          message={errorMessage(accounts.error, "Could not load your accounts.")}
          onRetry={() => {
            void accounts.refetch();
          }}
        />
      )}
      {accounts.data?.length === 0 && <EmptyState message="You have no accounts yet." />}
      {accounts.data && accounts.data.length > 0 && (
        <form className="card" onSubmit={(event) => void submit(event)}>
          <Field label="Account">
            {(control) => (
              <select
                {...control}
                value={chosen}
                onChange={(e) => {
                  setAccountId(e.target.value);
                }}
              >
                {accounts.data.map((a) => (
                  <option key={a.id} value={a.id}>
                    {accountTypeLabel(a.account_type)} {a.account_number_masked}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <div className="field-row">
            <Field label="From">
              {(control) => (
                <input
                  {...control}
                  type="date"
                  value={start}
                  max={today}
                  onChange={(e) => {
                    setStart(e.target.value);
                  }}
                  required
                />
              )}
            </Field>
            <Field label="To">
              {(control) => (
                <input
                  {...control}
                  type="date"
                  value={end}
                  max={today}
                  onChange={(e) => {
                    setEnd(e.target.value);
                  }}
                  required
                />
              )}
            </Field>
          </div>
          <fieldset className="choice-group">
            <legend>Format</legend>
            <label>
              <input
                type="radio"
                name="format"
                value="pdf"
                checked={format === "pdf"}
                onChange={() => {
                  setFormat("pdf");
                }}
              />{" "}
              PDF: a formatted statement to read or print
            </label>
            <label>
              <input
                type="radio"
                name="format"
                value="csv"
                checked={format === "csv"}
                onChange={() => {
                  setFormat("csv");
                }}
              />{" "}
              CSV: raw data for a spreadsheet
            </label>
          </fieldset>
          {error && <Alert tone="error">{error}</Alert>}
          {done && <Alert tone="success">{done}</Alert>}
          <Button type="submit" loading={busy}>
            Download statement
          </Button>
          <p className="muted">
            For your security we send you a notification each time a statement is downloaded.
          </p>
        </form>
      )}
    </div>
  );
}
