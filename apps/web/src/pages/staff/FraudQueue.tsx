import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDateTime } from "../../lib/format";
import { transactionStatus } from "../../lib/status";
import { Amount } from "../../ui/Amount";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert as Notice } from "../../ui/Alert";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { ALERT_STATUS_LABEL, DECISION_LABEL, canReviewFraud, riskTone } from "./fraudFormat";

const FILTERS = [
  { value: "OPEN", label: "Open" },
  { value: "", label: "All" },
  { value: "CONFIRMED_FRAUD", label: "Confirmed fraud" },
  { value: "FALSE_POSITIVE", label: "Released" },
] as const;

/** TASK-036: the analyst's alert queue (GET /fraud/alerts), newest first. */
export function FraudQueue() {
  const me = useCurrentUser();
  const allowed = canReviewFraud(me);
  const [status, setStatus] = useState<string>("OPEN");
  const alerts = useQuery({
    queryKey: ["fraud", "alerts", status],
    queryFn: () => api.fraudAlerts(status || undefined),
    enabled: allowed,
  });
  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Fraud alerts" />
        <Notice tone="error">
          Access restricted: fraud alerts need the Fraud Analyst or Administrator role.
        </Notice>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Fraud alerts"
        description="Transfers and bill payments the fraud engine held or blocked. Open one to see why."
      />
      <div className="filter-group" role="group" aria-label="Filter alerts">
        {FILTERS.map((f) => (
          <button
            key={f.value || "all"}
            type="button"
            className={
              status === f.value ? "btn btn--sm btn--primary" : "btn btn--sm btn--secondary"
            }
            aria-pressed={status === f.value}
            onClick={() => {
              setStatus(f.value);
            }}
          >
            {f.label}
          </button>
        ))}
      </div>
      {alerts.isPending && <LoadingState label="Loading alerts..." />}
      {alerts.isError && (
        <ErrorState
          message={errorMessage(alerts.error, "Could not load the alerts.")}
          onRetry={() => {
            void alerts.refetch();
          }}
        />
      )}
      {alerts.data?.length === 0 && (
        <EmptyState message={status === "OPEN" ? "No open alerts." : "No alerts."} />
      )}
      {alerts.data && alerts.data.length > 0 && (
        <div className="entries">
          <table>
            <caption className="sr-only">Fraud alerts, newest first</caption>
            <thead>
              <tr>
                <th scope="col">Raised</th>
                <th scope="col">Risk</th>
                <th scope="col">Decision</th>
                <th scope="col" className="right">
                  Amount
                </th>
                <th scope="col">Transaction</th>
                <th scope="col">Alert</th>
              </tr>
            </thead>
            <tbody>
              {alerts.data.map((a) => (
                <tr key={a.id}>
                  <td>
                    <Link to={`/staff/fraud/${a.id}`}>{formatDateTime(a.created_at)}</Link>
                  </td>
                  <td>
                    <StatusBadge
                      status={{ label: String(a.risk_score), tone: riskTone(a.risk_score) }}
                    />
                  </td>
                  <td>{DECISION_LABEL[a.decision] ?? a.decision}</td>
                  <td className="right">
                    <Amount minor={a.amount_minor} currency={a.currency} />
                  </td>
                  <td>
                    <StatusBadge status={transactionStatus(a.transaction_status)} />
                  </td>
                  <td>
                    <StatusBadge
                      status={ALERT_STATUS_LABEL[a.status] ?? { label: a.status, tone: "neutral" }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
