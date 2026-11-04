import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../../api/client";
import type {
  BehaviourProfile,
  CustomerInsightDetail,
  InteractionChannel,
  InteractionOutcome,
} from "../../api/client";
import { RATINGS } from "../../lib/analytics";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDate, formatDateTime, formatRelative } from "../../lib/format";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { Field } from "../../ui/Field";
import { ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { RmAssignmentControl } from "./RmAssignmentControl";
import {
  CHANNEL_LABEL,
  CHURN_LABEL,
  OUTCOME_LABEL,
  PRODUCT_TYPE_LABEL,
  declineText,
  durationText,
  inactivityText,
  score100,
  segmentLabel,
} from "./rmInsightsFormat";

const NOTE_MAX = 1000;
const LOG_LIMIT = 20;

interface CustomerFlyoutProps {
  customerId: string;
  name: string;
  onClose: () => void;
  /** ADMIN may assign the customer to a relationship manager (ADR-0032). */
  canAssign?: boolean;
}

/** TASK-066: one customer's detail and outreach log in a side panel (native <dialog>, so focus is
 * trapped and Esc closes it). Everything shown comes from the insights API; nothing is stored in
 * the browser. */
export function CustomerFlyout({
  customerId,
  name,
  onClose,
  canAssign = false,
}: CustomerFlyoutProps) {
  const detail = useQuery({
    queryKey: ["insights", "customer", customerId],
    queryFn: () => api.customerInsightDetail(customerId),
  });
  return (
    <Dialog open title={name} onClose={onClose} className="dialog--flyout">
      <Button variant="ghost" size="sm" className="flyout-close" onClick={onClose}>
        Close
      </Button>
      {detail.isPending && <LoadingState label="Loading customer details..." />}
      {detail.isError && (
        <ErrorState
          message={errorMessage(detail.error, "Could not load this customer.")}
          onRetry={() => {
            void detail.refetch();
          }}
        />
      )}
      {detail.data && (
        <>
          <RmAssignmentControl
            customerId={customerId}
            assigned={detail.data.assigned_rm}
            canAssign={canAssign}
          />
          <Indicators detail={detail.data} />
          <BehaviourProfileSection customerId={customerId} />
        </>
      )}
      <OutreachLog customerId={customerId} />
    </Dialog>
  );
}

function Indicators({ detail }: { detail: CustomerInsightDetail }) {
  const { insight, health, transaction_volume: volume } = detail;
  const churn = CHURN_LABEL[insight.churn_risk_level];
  const products = detail.recommendations ?? [];
  return (
    <>
      <p className="muted">
        {segmentLabel(insight.segment)} · customer since {formatDate(detail.customer_since)}
      </p>
      <section className="flyout-section" aria-labelledby="rm-churn">
        <h3 id="rm-churn">Churn risk</h3>
        <p>
          <StatusBadge status={{ label: churn.label, tone: churn.tone }} />{" "}
          <strong>{score100(insight.churn_risk_score)} out of 100</strong>
        </p>
        <dl className="details details--counts">
          <dt>Balance lost in the last 30 days</dt>
          <dd>{declineText(insight.balance_decline_rate)}</dd>
          <dt>Days since last activity</dt>
          <dd>{inactivityText(insight.inactivity_days)}</dd>
          <dt>Balance now</dt>
          <dd>
            <Amount minor={insight.total_balance_minor} currency="INR" />
          </dd>
        </dl>
        <p className="muted small">
          A rule-based score, not a measured probability: 55% from the balance decline and 45% from
          inactivity (60 days counts as fully inactive).
        </p>
      </section>
      <section className="flyout-section" aria-labelledby="rm-health">
        <h3 id="rm-health">Financial health</h3>
        {health ? (
          <p>
            <strong>{health.overall_score} out of 100</strong>{" "}
            <StatusBadge status={RATINGS[health.rating]} />
            {health.insufficient_data && (
              <span className="muted"> Less than 3 months of history, so treat it as early.</span>
            )}
          </p>
        ) : (
          <p className="muted">No account yet, so there is no health score.</p>
        )}
      </section>
      <section className="flyout-section" aria-labelledby="rm-volume">
        <h3 id="rm-volume">Last {volume.window_days} days</h3>
        <dl className="details details--counts">
          <dt>Transactions with other parties</dt>
          <dd>{countOf(volume.transaction_count, "transaction")}</dd>
          <dt>Money in</dt>
          <dd>
            <Amount minor={volume.inflow_minor} currency="INR" />
          </dd>
          <dt>Money out</dt>
          <dd>
            <Amount minor={volume.outflow_minor} currency="INR" />
          </dd>
        </dl>
      </section>
      <section className="flyout-section" aria-labelledby="rm-products">
        <h3 id="rm-products">Eligible products</h3>
        {products.length === 0 ? (
          <p className="muted">No product passes the eligibility rules for this customer yet.</p>
        ) : (
          <ul className="rm-products">
            {products.map((p) => (
              <li key={p.title}>
                <strong>{p.title}</strong>{" "}
                <span className="muted">
                  {PRODUCT_TYPE_LABEL[p.product_type] ?? p.product_type} · score {score100(p.score)}
                </span>
                <ul>
                  {p.reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

/** UC-11 profile (ADR-0057): only with the customer's analytics consent; otherwise the API says
 * only that there is none. */
function BehaviourProfileSection({ customerId }: { customerId: string }) {
  const profile = useQuery({
    queryKey: ["insights", "behaviour-profile", customerId],
    queryFn: () => api.customerBehaviourProfile(customerId),
  });
  return (
    <section className="flyout-section" aria-labelledby="rm-behaviour">
      <h3 id="rm-behaviour">Behavioural profile</h3>
      {profile.isPending && <LoadingState label="Loading the behavioural profile..." />}
      {profile.isError && (
        <ErrorState
          message={errorMessage(profile.error, "Could not load the behavioural profile.")}
          onRetry={() => {
            void profile.refetch();
          }}
        />
      )}
      {profile.data &&
        (profile.data.profile ? (
          <ProfileBody profile={profile.data.profile} />
        ) : (
          <p className="muted">
            The customer has not turned on usage analytics, so no behavioural profile is kept.
          </p>
        ))}
    </section>
  );
}

function ProfileBody({ profile }: { profile: BehaviourProfile }) {
  const { engagement, spend_income: money } = profile;
  return (
    <>
      <dl className="details details--counts">
        <dt>Segment</dt>
        <dd>{segmentLabel(profile.segment)}</dd>
        {money && (
          <>
            <dt>Monthly income</dt>
            <dd>
              <Amount minor={money.monthly_income_minor} currency={money.currency} />
            </dd>
            <dt>Monthly spend</dt>
            <dd>
              <Amount minor={money.monthly_spend_minor} currency={money.currency} />
            </dd>
          </>
        )}
        {engagement && (
          <>
            <dt>App sessions (last {engagement.window_days} days)</dt>
            <dd>{engagement.sessions}</dd>
            <dt>Average session</dt>
            <dd>{engagement.sessions ? durationText(engagement.avg_session_seconds) : "—"}</dd>
            <dt>Pages per session</dt>
            <dd>{engagement.pages_per_session}</dd>
            <dt>Last active</dt>
            <dd>
              {engagement.last_active_at ? formatRelative(engagement.last_active_at) : "Not yet"}
            </dd>
          </>
        )}
      </dl>
      {!engagement && (
        <p className="muted">App usage is summarised overnight; nothing has been summarised yet.</p>
      )}
      <h4>Product interests</h4>
      {profile.product_interests.length === 0 ? (
        <p className="muted">No interest in a product category shown yet.</p>
      ) : (
        <ul>
          {profile.product_interests.map((i) => (
            <li key={i.category}>
              <strong>{PRODUCT_TYPE_LABEL[i.category] ?? i.category}</strong>{" "}
              <span className="muted">
                score {i.score.toFixed(2)} · {countOf(i.signals, "signal")} · last{" "}
                {formatRelative(i.last_signal_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">
        Interest is a rule-based score: card opens weigh 3, loan calculator uses 2 and product page
        visits 1, halving every 14 days.
        {profile.computed_at && ` Updated ${formatRelative(profile.computed_at)}.`}
      </p>
    </>
  );
}

function OutreachLog({ customerId }: { customerId: string }) {
  const queryClient = useQueryClient();
  const [channel, setChannel] = useState<InteractionChannel>("CALL");
  const [outcome, setOutcome] = useState<InteractionOutcome>("FOLLOW_UP");
  const [note, setNote] = useState("");
  const [noteError, setNoteError] = useState<string | null>(null);
  const log = useQuery({
    queryKey: ["insights", "interactions", customerId],
    queryFn: () => api.customerInteractions(customerId, LOG_LIMIT),
  });
  const save = useMutation({
    mutationFn: () => api.logInteraction(customerId, { channel, outcome, note: note.trim() }),
    onSuccess: async () => {
      setNote("");
      await queryClient.invalidateQueries({ queryKey: ["insights", "interactions", customerId] });
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (note.trim() === "") {
      setNoteError("Write what was discussed.");
      return;
    }
    setNoteError(null);
    save.mutate();
  }

  return (
    <section className="flyout-section" aria-labelledby="rm-log">
      <h3 id="rm-log">Calls and notes</h3>
      <form className="rm-log-form" onSubmit={submit} noValidate>
        <Field label="Type">
          {(control) => (
            <select
              {...control}
              value={channel}
              onChange={(e) => {
                setChannel(e.target.value as InteractionChannel);
              }}
            >
              {(Object.keys(CHANNEL_LABEL) as InteractionChannel[]).map((c) => (
                <option key={c} value={c}>
                  {CHANNEL_LABEL[c]}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Outcome">
          {(control) => (
            <select
              {...control}
              value={outcome}
              onChange={(e) => {
                setOutcome(e.target.value as InteractionOutcome);
              }}
            >
              {(Object.keys(OUTCOME_LABEL) as InteractionOutcome[]).map((o) => (
                <option key={o} value={o}>
                  {OUTCOME_LABEL[o]}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field
          label="Note"
          hint={`Kept on the customer's record: the relationship manager who looks after them next can read it. ${String(NOTE_MAX - note.length)} characters left.`}
          error={noteError}
        >
          {(control) => (
            <textarea
              {...control}
              rows={3}
              maxLength={NOTE_MAX}
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
              }}
            />
          )}
        </Field>
        {save.isError && (
          <Alert tone="error">{errorMessage(save.error, "Could not save the note.")}</Alert>
        )}
        {save.isSuccess && note === "" && (
          <p className="muted" role="status">
            Saved.
          </p>
        )}
        <Button type="submit" loading={save.isPending}>
          {save.isPending ? "Saving..." : "Log it"}
        </Button>
      </form>
      {log.isPending && <LoadingState label="Loading the log..." />}
      {log.isError && (
        <ErrorState
          message={errorMessage(log.error, "Could not load the log.")}
          onRetry={() => {
            void log.refetch();
          }}
        />
      )}
      {log.data?.total === 0 && <p className="muted">Nothing logged for this customer yet.</p>}
      {log.data && log.data.total > 0 && (
        <>
          <p className="muted small">
            {log.data.total > log.data.items.length
              ? `Latest ${String(log.data.items.length)} of ${countOf(log.data.total, "entry", "entries")}`
              : countOf(log.data.total, "entry", "entries")}
          </p>
          <ul className="rm-log">
            {log.data.items.map((i) => (
              <li key={i.id}>
                <p>
                  <strong>{CHANNEL_LABEL[i.channel]}</strong> · {OUTCOME_LABEL[i.outcome]}
                </p>
                <p className="rm-log-note">{i.note}</p>
                <p className="muted small">
                  {i.logged_by} ·{" "}
                  <time dateTime={i.created_at} title={formatDateTime(i.created_at)}>
                    {formatRelative(i.created_at)}
                  </time>
                </p>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
