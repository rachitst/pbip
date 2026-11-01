import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { CustomerInsightItem } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { Field } from "../../ui/Field";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { CustomerFlyout } from "./CustomerFlyout";
import {
  CHURN_LABEL,
  SEGMENT_LABEL,
  canAccessRMInsights,
  inactivityText,
  score100,
  segmentLabel,
} from "./rmInsightsFormat";

export const PAGE_SIZE = 20;

/** Thresholds match the API's churn levels (MEDIUM from 0.40, HIGH from 0.70). */
const CHURN_FILTERS = [
  { value: "", label: "Any churn risk" },
  { value: "0.4", label: "Medium or high" },
  { value: "0.7", label: "High only" },
] as const;

const LOAN_FILTERS = [
  { value: "", label: "Any (no loan filter)" },
  { value: "0", label: "Eligible for a loan product" },
  { value: "0.5", label: "Loan propensity 50 or more" },
  { value: "0.7", label: "Loan propensity 70 or more" },
] as const;

function toNumber(value: string): number | undefined {
  return value === "" ? undefined : Number(value);
}

/** TASK-066: the Relationship Manager's leads, highest churn risk first (GET /insights/customers).
 * Every filter is applied by the API before paging, so a match on another page is never hidden. */
export function RMInsights() {
  const me = useCurrentUser();
  const allowed = canAccessRMInsights(me);
  const [segment, setSegment] = useState("");
  const [churn, setChurn] = useState("");
  const [loan, setLoan] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<CustomerInsightItem | null>(null);

  const filters = {
    segment: segment || undefined,
    min_churn_risk: toNumber(churn),
    min_loan_propensity: toNumber(loan),
    limit: PAGE_SIZE,
    offset,
  };
  const leads = useQuery({
    queryKey: ["insights", "customers", filters],
    queryFn: () => api.customerInsights(filters),
    enabled: allowed,
    placeholderData: keepPreviousData,
  });

  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Customer insights" />
        <Alert tone="error">
          Access restricted: customer insights need the Relationship Manager or Administrator role.
        </Alert>
      </div>
    );
  }

  function choose(setter: (value: string) => void) {
    return (value: string) => {
      setter(value);
      setOffset(0);
    };
  }

  const data = leads.data;
  const items = data?.items ?? [];
  return (
    <div className="page">
      <PageHeader
        title="Customer insights"
        description="Customers most at risk of leaving come first. Open one to see their health score, recent activity and eligible products, and to log a call or note."
      />
      <section className="rm-filters" aria-label="Filter customers">
        <FilterSelect
          label="Segment"
          value={segment}
          onChange={choose(setSegment)}
          options={[
            { value: "", label: "All segments" },
            ...Object.entries(SEGMENT_LABEL).map(([value, label]) => ({ value, label })),
          ]}
        />
        <FilterSelect
          label="Churn risk"
          value={churn}
          onChange={choose(setChurn)}
          options={CHURN_FILTERS}
        />
        <FilterSelect
          label="Loan propensity"
          value={loan}
          onChange={choose(setLoan)}
          options={LOAN_FILTERS}
        />
      </section>

      {leads.isPending && <LoadingState label="Loading customers..." />}
      {leads.isError && (
        <ErrorState
          message={errorMessage(leads.error, "Could not load customer insights.")}
          onRetry={() => {
            void leads.refetch();
          }}
        />
      )}
      {data?.total === 0 && (
        <EmptyState
          message={
            segment || churn || loan
              ? "No customers match these filters."
              : "There are no customers yet."
          }
        />
      )}
      {data && data.total > 0 && (
        <>
          <p className="muted rm-summary" aria-live="polite">
            Showing {offset + 1}-{offset + items.length} of {countOf(data.total, "customer")}.
            Scores as of {formatDateTime(data.evaluated_at)}.
          </p>
          <LeadsTable
            items={items}
            onOpen={(item) => {
              setSelected(item);
            }}
          />
          <div className="rm-pager">
            <Button
              variant="secondary"
              size="sm"
              disabled={offset === 0}
              onClick={() => {
                setOffset(Math.max(0, offset - PAGE_SIZE));
              }}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => {
                setOffset(offset + PAGE_SIZE);
              }}
            >
              Next
            </Button>
          </div>
        </>
      )}
      {selected && (
        <CustomerFlyout
          key={selected.customer_id}
          customerId={selected.customer_id}
          name={selected.full_name}
          onClose={() => {
            setSelected(null);
          }}
        />
      )}
    </div>
  );
}

interface FilterSelectProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: readonly { value: string; label: string }[];
}

function FilterSelect({ label, value, onChange, options }: FilterSelectProps) {
  return (
    <Field label={label}>
      {(control) => (
        <select
          {...control}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

function LoanCell({ item }: { item: CustomerInsightItem }) {
  const score = item.loan_propensity_score;
  if (typeof score !== "number" || !item.loan_product) {
    return <span className="muted">No eligible loan</span>;
  }
  return (
    <>
      {score100(score)} <span className="muted">({item.loan_product})</span>
    </>
  );
}

function LeadsTable({
  items,
  onOpen,
}: {
  items: CustomerInsightItem[];
  onOpen: (item: CustomerInsightItem) => void;
}) {
  return (
    <div className="entries">
      <table>
        <caption className="sr-only">Customers, highest churn risk first</caption>
        <thead>
          <tr>
            <th scope="col">Customer</th>
            <th scope="col">Segment</th>
            <th scope="col" className="right">
              Balance
            </th>
            <th scope="col">Churn risk</th>
            <th scope="col">Last active</th>
            <th scope="col">Loan propensity</th>
            <th scope="col" className="right">
              Details
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const churn = CHURN_LABEL[item.churn_risk_level];
            return (
              <tr key={item.customer_id}>
                <td>
                  {item.full_name}
                  {item.high_value && (
                    <>
                      {" "}
                      <StatusBadge status={{ label: "High value", tone: "info" }} />
                    </>
                  )}
                </td>
                <td>{segmentLabel(item.segment)}</td>
                <td className="right">
                  <Amount minor={item.total_balance_minor} currency="INR" />
                </td>
                <td>
                  <StatusBadge
                    status={{
                      label: `${churn.label} · ${String(score100(item.churn_risk_score))}`,
                      tone: churn.tone,
                    }}
                  />
                </td>
                <td>
                  {item.inactivity_days === 0
                    ? "Today"
                    : `${inactivityText(item.inactivity_days)} ago`}
                </td>
                <td>
                  <LoanCell item={item} />
                </td>
                <td className="right">
                  <Button
                    variant="secondary"
                    size="sm"
                    aria-label={`Open ${item.full_name}`}
                    onClick={() => {
                      onOpen(item);
                    }}
                  >
                    Open
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
