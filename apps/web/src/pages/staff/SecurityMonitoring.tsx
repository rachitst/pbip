import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../../api/client";
import type { SecurityMonitoring as Monitoring } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime, formatDuration, formatRelative } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert as Notice } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { ALERT_STATUS_LABEL, DECISION_LABEL, canReviewFraud, riskTone } from "./fraudFormat";
import type { Tone } from "../../lib/status";

/** The API caches the panels for 60 seconds; polling faster would only re-read the cache. */
const REFRESH_MS = 60_000;

const SEVERITY: { key: string; label: string; tone: Tone }[] = [
  { key: "HIGH", label: "High", tone: "danger" },
  { key: "MEDIUM", label: "Medium", tone: "warning" },
];

/** Security event types counted in the login panel, in words. */
const SECURITY_COUNTS: { key: string; label: string }[] = [
  { key: "LOGIN_FAILURE", label: "Failed sign-ins" },
  { key: "ACCOUNT_LOCKED", label: "Accounts locked" },
  { key: "LOGIN_BLOCKED_LOCKED", label: "Sign-ins while locked" },
  { key: "MFA_FAILURE", label: "Wrong authenticator codes" },
  { key: "STEP_UP_FAILURE", label: "Failed payment verifications" },
];

/** Ids are shown short (the first block of the UUID); the full id is in the tooltip. */
function ShortId({ id }: { id: string }) {
  return (
    <code className="short-id" title={id}>
      {id.slice(0, 8)}
    </code>
  );
}

function RiskBadge({ score }: { score: number }) {
  return <StatusBadge status={{ label: String(score), tone: riskTone(score) }} />;
}

export function OpenAlertsPanel({ panel }: { panel: Monitoring["open_alerts"] }) {
  return (
    <section className="card fraud-panel" aria-labelledby="mon-open">
      <h2 id="mon-open">Open investigations</h2>
      {panel.total === 0 ? (
        <EmptyState message="No open alerts. Nothing is waiting for an analyst." />
      ) : (
        <>
          <p>
            <strong>{countOf(panel.total, "open alert")}</strong>
            {panel.oldest_open_age_seconds !== null && (
              <>
                {"; the oldest has waited "}
                <strong>{formatDuration(panel.oldest_open_age_seconds)}</strong>
              </>
            )}
            .
          </p>
          <ul className="severity-list">
            {SEVERITY.map((s) => {
              const n = panel.by_severity[s.key] ?? 0;
              return (
                <li key={s.key}>
                  <span>
                    {s.label}: {n}
                  </span>
                  <progress
                    className={`meter meter--${s.tone}`}
                    max={panel.total}
                    value={n}
                    aria-label={`${s.label} severity: ${countOf(n, "open alert")} of ${panel.total}`}
                  />
                </li>
              );
            })}
          </ul>
          <div className="entries">
            <table>
              <caption className="sr-only">Longest-waiting open alerts, oldest first</caption>
              <thead>
                <tr>
                  <th scope="col">Raised</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Risk</th>
                </tr>
              </thead>
              <tbody>
                {panel.oldest.map((a) => (
                  <tr key={a.alert_id}>
                    <td>
                      <Link to={`/staff/fraud/${a.alert_id}`} title={formatDateTime(a.created_at)}>
                        {formatRelative(a.created_at)}
                      </Link>
                    </td>
                    <td>
                      <StatusBadge
                        status={{
                          label: a.severity === "HIGH" ? "High" : "Medium",
                          tone: a.severity === "HIGH" ? "danger" : "warning",
                        }}
                      />
                    </td>
                    <td>
                      <RiskBadge score={a.risk_score} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <p>
        <Link to="/staff/fraud">Open the alert queue</Link>
      </p>
    </section>
  );
}

export function HighRiskCustomersPanel({
  customers,
  windowDays,
}: {
  customers: Monitoring["high_risk_customers"];
  windowDays: number;
}) {
  return (
    <section className="card fraud-panel monitoring-wide" aria-labelledby="mon-customers">
      <h2 id="mon-customers">High-risk customers</h2>
      <p className="muted">
        Customers with the most held, blocked or escalated payments in the last{" "}
        {countOf(windowDays, "day")}. Open an alert to see who they are.
      </p>
      {customers.length === 0 ? (
        <EmptyState message={`No flagged payments in the last ${countOf(windowDays, "day")}.`} />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">High-risk customers, most flagged first</caption>
            <thead>
              <tr>
                <th scope="col">Customer</th>
                <th scope="col" className="right">
                  Flagged
                </th>
                <th scope="col" className="right">
                  Blocked
                </th>
                <th scope="col">Highest risk</th>
                <th scope="col" className="right">
                  Open alerts
                </th>
                <th scope="col">Last flagged</th>
                <th scope="col">Alert</th>
              </tr>
            </thead>
            <tbody>
              {customers.map((c) => (
                <tr key={c.customer_id}>
                  <td>
                    <ShortId id={c.customer_id} />
                  </td>
                  <td className="right">{c.flagged_events}</td>
                  <td className="right">{c.blocked}</td>
                  <td>
                    <RiskBadge score={c.max_risk} />
                  </td>
                  <td className="right">{c.open_alerts}</td>
                  <td title={formatDateTime(c.last_flagged_at)}>
                    {formatRelative(c.last_flagged_at)}
                  </td>
                  <td>
                    {c.alert_id ? (
                      <Link
                        to={`/staff/fraud/${c.alert_id}`}
                        aria-label={`Open the alert for customer ${c.customer_id.slice(0, 8)}`}
                      >
                        {c.open_alerts > 0 ? "Review" : "View"}
                      </Link>
                    ) : (
                      <span className="muted">No alert</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function LoginAnomaliesPanel({ panel }: { panel: Monitoring["login_anomalies"] }) {
  const period = countOf(panel.window_hours, "hour");
  return (
    <section className="card fraud-panel" aria-labelledby="mon-logins">
      <h2 id="mon-logins">Login anomalies</h2>
      <p className="muted">Last {period}. User ids only; no addresses or emails.</p>
      <dl className="details">
        {SECURITY_COUNTS.map((c) => (
          <div className="details__row" key={c.key}>
            <dt>{c.label}</dt>
            <dd>{panel.counts[c.key] ?? 0}</dd>
          </div>
        ))}
        <div className="details__row">
          <dt>Failed sign-ins for unknown emails</dt>
          <dd>{panel.unknown_account_failures}</dd>
        </div>
      </dl>
      <h3>Failed sign-in bursts</h3>
      {panel.bursts.length === 0 ? (
        <EmptyState
          message={`No user had ${panel.burst_threshold} or more failed sign-ins in the last ${period}.`}
        />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">
              Users with {panel.burst_threshold} or more failed sign-ins
            </caption>
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col" className="right">
                  Failures
                </th>
                <th scope="col">Last failure</th>
              </tr>
            </thead>
            <tbody>
              {panel.bursts.map((b) => (
                <tr key={b.user_id}>
                  <td>
                    <ShortId id={b.user_id} />
                  </td>
                  <td className="right">{b.failures}</td>
                  <td title={formatDateTime(b.last_failure_at)}>
                    {formatRelative(b.last_failure_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>Recent lockouts</h3>
      {panel.recent_lockouts.length === 0 ? (
        <EmptyState message="No accounts have been locked." />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">Newest account lockouts</caption>
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Locked</th>
              </tr>
            </thead>
            <tbody>
              {panel.recent_lockouts.map((l, i) => (
                <tr key={`${l.user_id ?? "deleted"}-${l.locked_at}-${String(i)}`}>
                  <td>
                    {l.user_id ? (
                      <ShortId id={l.user_id} />
                    ) : (
                      <span className="muted">Deleted user</span>
                    )}
                  </td>
                  <td title={formatDateTime(l.locked_at)}>{formatRelative(l.locked_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function FraudEventsPanel({ panel }: { panel: Monitoring["fraud_events"] }) {
  const held = panel.last_24h.REVIEW ?? 0;
  const blocked = panel.last_24h.BLOCK ?? 0;
  return (
    <section className="card fraud-panel monitoring-wide" aria-labelledby="mon-events">
      <h2 id="mon-events">Recent fraud events</h2>
      <p>
        Last 24 hours: <strong>{countOf(held, "payment")} held for review</strong>,{" "}
        <strong>{blocked} blocked</strong>.
      </p>
      {panel.latest.length === 0 ? (
        <EmptyState message="No held, blocked or escalated payments yet." />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">The newest flagged fraud decisions</caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Decision</th>
                <th scope="col">Risk</th>
                <th scope="col" className="right">
                  Amount
                </th>
                <th scope="col">Customer</th>
                <th scope="col">Alert</th>
              </tr>
            </thead>
            <tbody>
              {panel.latest.map((e) => (
                <tr key={e.event_id}>
                  <td title={formatDateTime(e.created_at)}>
                    {e.alert_id ? (
                      <Link to={`/staff/fraud/${e.alert_id}`}>{formatRelative(e.created_at)}</Link>
                    ) : (
                      formatRelative(e.created_at)
                    )}
                  </td>
                  <td>{DECISION_LABEL[e.decision] ?? e.decision}</td>
                  <td>
                    <RiskBadge score={e.risk_score} />
                  </td>
                  <td className="right">
                    <Amount minor={e.amount_minor} currency={e.currency} />
                  </td>
                  <td>
                    <ShortId id={e.customer_id} />
                  </td>
                  <td>
                    {e.alert_status ? (
                      <StatusBadge
                        status={
                          ALERT_STATUS_LABEL[e.alert_status] ?? {
                            label: e.alert_status,
                            tone: "neutral",
                          }
                        }
                      />
                    ) : (
                      <span className="muted">No alert</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** TASK-099 (UC-13): one staff view of open investigations, high-risk customers, login anomalies
 * and flagged fraud decisions (GET /fraud/monitoring). Drill-downs go to the alert queue. */
export function SecurityMonitoring() {
  const me = useCurrentUser();
  const allowed = canReviewFraud(me);
  const monitoring = useQuery({
    queryKey: ["fraud", "monitoring"],
    queryFn: api.securityMonitoring,
    enabled: allowed,
    refetchInterval: REFRESH_MS,
  });
  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Security monitoring" />
        <Notice tone="error">
          Access restricted: security monitoring needs the Fraud Analyst or Administrator role.
        </Notice>
      </div>
    );
  }
  const data = monitoring.data;
  return (
    <div className="page">
      <PageHeader
        title="Security monitoring"
        description="Open investigations, high-risk customers, login anomalies and flagged payments. Refreshes every minute."
        actions={
          data && (
            <span className="muted" title={formatDateTime(data.generated_at)}>
              Updated {formatRelative(data.generated_at)}
            </span>
          )
        }
      />
      {monitoring.isPending && <LoadingState label="Loading security monitoring..." />}
      {monitoring.isError && (
        <ErrorState
          message={errorMessage(monitoring.error, "Could not load security monitoring.")}
          onRetry={() => {
            void monitoring.refetch();
          }}
        />
      )}
      {data && (
        <div className="monitoring-grid">
          <OpenAlertsPanel panel={data.open_alerts} />
          <LoginAnomaliesPanel panel={data.login_anomalies} />
          <HighRiskCustomersPanel
            customers={data.high_risk_customers}
            windowDays={data.high_risk_window_days}
          />
          <FraudEventsPanel panel={data.fraud_events} />
        </div>
      )}
    </div>
  );
}
