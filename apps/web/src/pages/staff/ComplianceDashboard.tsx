import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../../api/client";
import type {
  ComplianceExceptionsOut,
  ExceptionKind,
  ExceptionOut,
  ExceptionStatus,
  UserOut,
} from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime, formatDuration, formatRelative } from "../../lib/format";
import type { Tone } from "../../lib/status";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { Field } from "../../ui/Field";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { FilterSelect, Metric, PERIOD_OPTIONS } from "./dashboardParts";
import "./dashboards.css";

/** The compliance endpoints admit COMPLIANCE_OFFICER and ADMIN (with an MFA-verified session). */
export function canManageCompliance(me: UserOut): boolean {
  return me.roles.includes("COMPLIANCE_OFFICER") || me.roles.includes("ADMIN");
}

/** Typed by the OpenAPI enum: a kind the API adds fails the type check until it is described. */
export const KIND_LABEL: Record<ExceptionKind, string> = {
  BLOCKED_TRANSFER: "Blocked payment",
  ACCOUNT_LOCKOUT: "Sign-in lockout",
  AUDIT_CHAIN_FAILURE: "Audit chain failure",
};

export const STATUS_VIEW: Record<ExceptionStatus, { label: string; tone: Tone }> = {
  OPEN: { label: "Open", tone: "danger" },
  ACKNOWLEDGED: { label: "Acknowledged", tone: "warning" },
  RESOLVED: { label: "Resolved", tone: "success" },
};

const KINDS = Object.keys(KIND_LABEL) as ExceptionKind[];
const STATUSES = Object.keys(STATUS_VIEW) as ExceptionStatus[];

const KIND_OPTIONS = [
  { value: "", label: "All kinds" },
  ...KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] })),
] as const;
const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  ...STATUSES.map((s) => ({ value: s, label: STATUS_VIEW[s].label })),
] as const;

type Action = "acknowledge" | "resolve";

function hours(n: number): string {
  return countOf(n, "hour");
}

export function Indicators({ data }: { data: ComplianceExceptionsOut }) {
  const ind = data.indicators;
  const period = countOf(ind.window_days, "day");
  const chain = data.audit_chain;
  return (
    <section className="card" aria-labelledby="compliance-indicators">
      <h2 id="compliance-indicators">Regulatory monitoring</h2>
      <dl className="metric-grid">
        <Metric
          label="Unresolved exceptions"
          value={ind.unresolved_exceptions}
          tone={ind.unresolved_exceptions > 0 ? "warning" : undefined}
          definition={`Open or acknowledged exceptions that occurred in the last ${period} (an audit chain failure counts whatever its age).`}
        />
        <Metric
          label="Not acknowledged within SLA"
          value={ind.unacknowledged_past_sla}
          tone={ind.unacknowledged_past_sla > 0 ? "danger" : undefined}
          definition={`Open exceptions from the last ${period} that occurred more than ${hours(ind.acknowledge_sla_hours)} ago.`}
        />
        <Metric
          label="Fraud reviews pending"
          value={ind.reviews_pending}
          definition="Fraud alerts still open (held or blocked payments waiting for an analyst), of any age."
        />
        <Metric
          label="Fraud reviews past SLA"
          value={ind.reviews_past_sla}
          tone={ind.reviews_past_sla > 0 ? "danger" : undefined}
          definition={`Open fraud alerts raised more than ${hours(ind.review_sla_hours)} ago.${
            ind.oldest_review_at
              ? ` The oldest was raised ${formatRelative(ind.oldest_review_at)}.`
              : ""
          }`}
        />
        <Metric
          label="Audit chain"
          value={chain.valid ? "Verified" : `Broken at #${String(chain.first_invalid_seq)}`}
          tone={chain.valid ? undefined : "danger"}
          definition={`Every audit entry's hash re-computed just now: ${countOf(chain.checked, "entry", "entries")} verified${chain.valid ? "" : " before the break"}.`}
        />
      </dl>
      <div className="entries">
        <table>
          <caption>
            Exceptions by kind and status, last {period}. Service levels are set by the bank: an
            exception should be acknowledged within {hours(ind.acknowledge_sla_hours)}, a fraud
            review decided within {hours(ind.review_sla_hours)}.
          </caption>
          <thead>
            <tr>
              <th scope="col">Kind</th>
              {STATUSES.map((s) => (
                <th key={s} scope="col" className="right">
                  {STATUS_VIEW[s].label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {KINDS.map((k) => (
              <tr key={k}>
                <th scope="row">{KIND_LABEL[k]}</th>
                {STATUSES.map((s) => (
                  <td key={s} className="right">
                    {ind.by_kind[k][s]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Detail({ item }: { item: ExceptionOut }) {
  if (item.kind === "AUDIT_CHAIN_FAILURE") {
    return <>First failing entry #{item.first_invalid_seq ?? item.ref}</>;
  }
  if (item.kind === "ACCOUNT_LOCKOUT") {
    return <span className="muted">Too many failed sign-ins</span>;
  }
  return (
    <>
      {item.amount_minor !== null && item.amount_minor !== undefined && item.currency ? (
        <Amount minor={item.amount_minor} currency={item.currency} />
      ) : null}{" "}
      <span className="muted">
        risk {item.risk_score ?? "?"}
        {item.reason_codes && item.reason_codes.length > 0
          ? ` · ${item.reason_codes.join(", ")}`
          : ""}
      </span>
    </>
  );
}

function Steps({ item }: { item: ExceptionOut }) {
  const steps = [
    ["Acknowledged", item.acknowledged],
    ["Resolved", item.resolved],
  ] as const;
  const done = steps.filter(([, step]) => step);
  if (done.length === 0) return <span className="muted">No action yet</span>;
  return (
    <ul className="plain-list">
      {done.map(([label, step]) =>
        step ? (
          <li key={label}>
            {label} {formatRelative(step.at)}: “{step.reason}”
          </li>
        ) : null,
      )}
    </ul>
  );
}

export function ExceptionsTable({
  items,
  onAct,
}: {
  items: ExceptionOut[];
  onAct: (item: ExceptionOut, action: Action) => void;
}) {
  return (
    <div className="entries">
      <table>
        <caption className="sr-only">Exceptions, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Occurred</th>
            <th scope="col">Kind</th>
            <th scope="col">Detail</th>
            <th scope="col">Status</th>
            <th scope="col">History</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td title={formatDateTime(item.occurred_at)}>{formatRelative(item.occurred_at)}</td>
              <td>
                {KIND_LABEL[item.kind]}{" "}
                <code className="short-id" title={item.ref}>
                  {item.ref.slice(0, 8)}
                </code>
              </td>
              <td>
                <Detail item={item} />
              </td>
              <td>
                <StatusBadge status={STATUS_VIEW[item.status]} />
              </td>
              <td>
                <Steps item={item} />
              </td>
              <td className="row-actions">
                {item.status === "OPEN" && (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => {
                      onAct(item, "acknowledge");
                    }}
                  >
                    Acknowledge
                  </Button>
                )}
                {item.status !== "RESOLVED" && (
                  <Button
                    size="sm"
                    onClick={() => {
                      onAct(item, "resolve");
                    }}
                  >
                    Resolve
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ActionDialog({
  target,
  onClose,
}: {
  target: { item: ExceptionOut; action: Action } | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const act = useMutation({
    mutationFn: (t: { item: ExceptionOut; action: Action; reason: string }) =>
      api.actOnException(t.item.kind, t.item.ref, t.action, t.reason),
    onSuccess: () => {
      setReason("");
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["compliance", "exceptions"] });
    },
  });
  const verb = target?.action === "resolve" ? "Resolve" : "Acknowledge";
  const trimmed = reason.trim();
  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (target && trimmed.length >= 3) act.mutate({ ...target, reason: trimmed });
  }
  return (
    <Dialog
      open={target !== null}
      title={`${verb} ${target ? KIND_LABEL[target.item.kind].toLowerCase() : "exception"}`}
      onClose={() => {
        act.reset();
        onClose();
      }}
    >
      <form onSubmit={submit}>
        <Field
          label="Reason"
          hint="Recorded in the audit log with your name; 3 to 500 characters."
          error={act.isError ? errorMessage(act.error, "Could not save the action.") : null}
        >
          {(control) => (
            <textarea
              {...control}
              value={reason}
              maxLength={500}
              rows={3}
              onChange={(e) => {
                setReason(e.target.value);
              }}
            />
          )}
        </Field>
        <div className="dialog__actions">
          <Button
            variant="ghost"
            onClick={() => {
              act.reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button type="submit" loading={act.isPending} disabled={trimmed.length < 3}>
            {verb}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** TASK-100 (UC-12 Compliance & Risk): system-generated exceptions with acknowledge and resolve,
 * and regulatory indicators (GET /compliance/exceptions). Every action is audited server-side. */
export function ComplianceDashboard() {
  const me = useCurrentUser();
  const allowed = canManageCompliance(me);
  const [days, setDays] = useState<"7" | "30" | "90">("30");
  const [kind, setKind] = useState<ExceptionKind | "">("");
  const [status, setStatus] = useState<ExceptionStatus | "">("");
  const [target, setTarget] = useState<{ item: ExceptionOut; action: Action } | null>(null);
  const filters = { days: Number(days), kind: kind || undefined, status: status || undefined };
  const query = useQuery({
    queryKey: ["compliance", "exceptions", filters],
    queryFn: () => api.complianceExceptions(filters),
    enabled: allowed,
  });
  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Compliance & risk" />
        <Alert tone="error">
          Access restricted: the compliance dashboard needs the Compliance Officer or Administrator
          role.
        </Alert>
      </div>
    );
  }
  const data = query.data;
  return (
    <div className="page dashboard-page">
      <PageHeader
        title="Compliance & risk"
        description="Blocked payments, sign-in lockouts and audit chain failures to acknowledge and resolve, with service-level indicators."
        actions={
          data && (
            <span className="muted" title={formatDateTime(data.generated_at)}>
              Updated {formatRelative(data.generated_at)}
            </span>
          )
        }
      />
      <section className="rm-filters" aria-label="Filter exceptions">
        <FilterSelect label="Period" value={days} onChange={setDays} options={PERIOD_OPTIONS} />
        <FilterSelect label="Kind" value={kind} onChange={setKind} options={KIND_OPTIONS} />
        <FilterSelect label="Status" value={status} onChange={setStatus} options={STATUS_OPTIONS} />
      </section>
      {query.isPending && <LoadingState label="Loading compliance exceptions..." />}
      {query.isError && (
        <ErrorState
          message={errorMessage(query.error, "Could not load the compliance dashboard.")}
          onRetry={() => {
            void query.refetch();
          }}
        />
      )}
      {data && (
        <>
          <Indicators data={data} />
          <section className="card" aria-labelledby="compliance-exceptions">
            <h2 id="compliance-exceptions">Exceptions</h2>
            {data.indicators.oldest_review_at && data.indicators.reviews_past_sla > 0 && (
              <p className="muted">
                The oldest open fraud review has waited{" "}
                {formatDuration(
                  Math.max(
                    0,
                    Math.round(
                      (Date.parse(data.generated_at) -
                        Date.parse(data.indicators.oldest_review_at)) /
                        1000,
                    ),
                  ),
                )}
                ; fraud analysts decide it in the alert queue.
              </p>
            )}
            {data.items.length === 0 ? (
              <EmptyState
                message={
                  kind || status
                    ? "No exceptions match these filters."
                    : `No exceptions in the last ${countOf(data.indicators.window_days, "day")}.`
                }
              />
            ) : (
              <ExceptionsTable
                items={data.items}
                onAct={(item, action) => {
                  setTarget({ item, action });
                }}
              />
            )}
          </section>
        </>
      )}
      <ActionDialog
        target={target}
        onClose={() => {
          setTarget(null);
        }}
      />
    </div>
  );
}
