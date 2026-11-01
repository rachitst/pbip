import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "../../api/client";
import type { AlertDetail } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDate, formatDateTime } from "../../lib/format";
import { transactionStatus } from "../../lib/status";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { Field } from "../../ui/Field";
import { PageHeader } from "../../ui/PageHeader";
import { useCurrentUser } from "../../shell/MeContext";
import { ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import {
  ALERT_STATUS_LABEL,
  DECISION_LABEL,
  SOURCE_LABEL,
  barWidths,
  contributionLabel,
  parseReasons,
  canReviewFraud,
  plainMessage,
  riskTone,
} from "./fraudFormat";
import type { FraudReason } from "./fraudFormat";

type Action = "RELEASE" | "REJECT";

const ACTION_TEXT: Record<Action, { title: string; confirm: string; effect: string }> = {
  RELEASE: {
    title: "Release as a false positive?",
    confirm: "Release",
    effect: "The held money is captured and the payment is posted.",
  },
  REJECT: {
    title: "Reject as confirmed fraud?",
    confirm: "Reject",
    effect: "The payment is cancelled and the held money returns to the customer.",
  },
};

function ReasonList({ reasons }: { reasons: FraudReason[] }) {
  const widths = barWidths(reasons);
  return (
    <ul className="reason-list">
      {reasons.map((r) => (
        <li key={`${r.source}-${r.code}`} className="reason">
          <div className="reason__head">
            <span className={`badge badge--${r.source === "RULE" ? "info" : "neutral"}`}>
              {SOURCE_LABEL[r.source]}
            </span>
            <span className="reason__text">{plainMessage(r)}</span>
          </div>
          {r.contribution !== null && (
            <div className="reason__bar">
              <progress
                className={`meter meter--${r.source === "RULE" ? "info" : "warning"}`}
                max={100}
                value={widths.get(r) ?? 0}
                aria-label={`${plainMessage(r)}: ${contributionLabel(r)}`}
              />
              <span className="muted">{contributionLabel(r)}</span>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function Context({ alert }: { alert: AlertDetail }) {
  return (
    <dl className="details">
      <dt>Amount</dt>
      <dd>
        <Amount minor={alert.amount_minor} currency={alert.currency} />
      </dd>
      <dt>Type</dt>
      <dd>{alert.transaction_type === "BILL_PAYMENT" ? "Bill payment" : "Transfer"}</dd>
      <dt>Reference</dt>
      <dd>{alert.reference ?? "—"}</dd>
      <dt>Transaction status</dt>
      <dd>
        <StatusBadge status={transactionStatus(alert.transaction_status)} />
      </dd>
      <dt>Raised</dt>
      <dd>{formatDateTime(alert.created_at)}</dd>
      <dt>Scored by</dt>
      <dd>
        {alert.model_version} in {alert.latency_ms.toFixed(1)} ms
      </dd>
    </dl>
  );
}

/** TASK-036: why an alert was raised (reasons in plain language, SHAP bars) and the decision. */
export function FraudAlert() {
  const { alertId = "" } = useParams();
  const allowed = canReviewFraud(useCurrentUser());
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [note, setNote] = useState("");
  const alert = useQuery({
    queryKey: ["fraud", "alert", alertId],
    queryFn: () => api.fraudAlert(alertId),
    enabled: allowed,
  });
  const decide = useMutation({
    mutationFn: (chosen: Action) => api.decideAlert(alertId, chosen, note),
    onSuccess: async () => {
      setAction(null);
      await queryClient.invalidateQueries({ queryKey: ["fraud"] });
    },
  });

  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Fraud alert" />
        <Alert tone="error">
          Access restricted: fraud alerts need the Fraud Analyst or Administrator role.
        </Alert>
      </div>
    );
  }
  if (alert.isPending) return <LoadingState label="Loading the alert..." />;
  if (alert.isError) {
    return (
      <div className="page">
        <ErrorState
          message={errorMessage(alert.error, "Could not load this alert.")}
          onRetry={() => {
            void alert.refetch();
          }}
        />
        <Link to="/staff/fraud">Back to alerts</Link>
      </div>
    );
  }
  const a = alert.data;
  const reasons = parseReasons(a.reasons);
  const open = a.status === "OPEN";
  const status = ALERT_STATUS_LABEL[a.status] ?? { label: a.status, tone: "neutral" as const };
  return (
    <div className="page">
      <PageHeader
        title={`Risk ${a.risk_score}: ${DECISION_LABEL[a.decision] ?? a.decision}`}
        description="Every point of the score is explained below."
        actions={<StatusBadge status={status} />}
      />
      <p>
        <Link to="/staff/fraud">← Back to alerts</Link>
      </p>
      {decide.isSuccess && <Alert tone="success">Decision recorded.</Alert>}
      <div className="fraud-grid">
        <section className="card fraud-panel" aria-labelledby="why">
          <h2 id="why">Why it was flagged</h2>
          <progress
            className={`meter meter--${riskTone(a.risk_score)}`}
            max={100}
            value={a.risk_score}
            aria-label={`Risk score ${a.risk_score} of 100`}
          />
          {reasons.length === 0 ? (
            <p className="muted">No reasons were recorded for this decision.</p>
          ) : (
            <ReasonList reasons={reasons} />
          )}
          <p className="muted">
            Bank rules add points. Model bars are SHAP values: how far each factor pushed the model
            towards fraud, relative to the strongest one.
          </p>
        </section>
        <div className="fraud-stack">
          <section className="card fraud-panel" aria-labelledby="customer">
            <h2 id="customer">Customer</h2>
            {a.customer ? (
              <dl className="details">
                <dt>Name</dt>
                <dd>{a.customer.full_name}</dd>
                <dt>KYC</dt>
                <dd>{a.customer.kyc_status}</dd>
                <dt>Segment</dt>
                <dd>{a.customer.segment ?? "—"}</dd>
                <dt>Customer since</dt>
                <dd>{formatDate(a.customer.customer_since)}</dd>
              </dl>
            ) : (
              <p className="muted">Customer record not found.</p>
            )}
          </section>
          <section className="card fraud-panel" aria-labelledby="txn">
            <h2 id="txn">Transaction</h2>
            <Context alert={a} />
          </section>
        </div>
      </div>
      {open ? (
        <section className="card fraud-panel fraud-decision" aria-labelledby="decide">
          <h2 id="decide">Decision</h2>
          <Field label="Note (optional, kept in the audit trail)">
            {(control) => (
              <textarea
                {...control}
                maxLength={1000}
                value={note}
                onChange={(e) => {
                  setNote(e.target.value);
                }}
              />
            )}
          </Field>
          <div className="fraud-actions">
            <Button
              onClick={() => {
                setAction("RELEASE");
              }}
            >
              Release
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setAction("REJECT");
              }}
            >
              Reject
            </Button>
          </div>
        </section>
      ) : (
        <Alert tone="info">
          Resolved {a.resolved_at ? formatDateTime(a.resolved_at) : ""}
          {a.resolution_note ? `: ${a.resolution_note}` : "."}
        </Alert>
      )}
      <ConfirmDialog
        open={action !== null}
        title={action ? ACTION_TEXT[action].title : ""}
        items={[
          { label: "Amount", value: <Amount minor={a.amount_minor} currency={a.currency} /> },
          { label: "Effect", value: action ? ACTION_TEXT[action].effect : "" },
        ]}
        confirmLabel={action ? ACTION_TEXT[action].confirm : ""}
        tone={action === "REJECT" ? "danger" : "primary"}
        busy={decide.isPending}
        error={decide.isError ? errorMessage(decide.error, "The decision was not saved.") : null}
        onConfirm={() => {
          if (action) decide.mutate(action);
        }}
        onCancel={() => {
          setAction(null);
          decide.reset();
        }}
      />
    </div>
  );
}
