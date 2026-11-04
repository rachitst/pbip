import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../../api/client";
import type { FraudStats } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime, formatRelative } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { TOOLTIP_STYLE } from "../insightsChart";
import { FilterSelect, Metric, PERIOD_OPTIONS } from "./dashboardParts";
import { SOURCE_LABEL, canReviewFraud } from "./fraudFormat";
import type { FraudReason } from "./fraudFormat";
import { DECISION_SERIES, bandLabel, dailyRows, formatRate, reasonLabel } from "./fraudStatsChart";
import type { DayRow } from "./fraudStatsChart";
import "./dashboards.css";

/** The API caches each window for 60 seconds; polling faster would only re-read the cache. */
const REFRESH_MS = 60_000;

function sourceLabel(source: string | null): string {
  return source && source in SOURCE_LABEL ? SOURCE_LABEL[source as FraudReason["source"]] : "-";
}

function Legend() {
  return (
    <ul className="insights-legend fraud-dash-legend" aria-label="Chart key">
      {DECISION_SERIES.map((s) => (
        <li key={s.key} className="fraud-dash-legend__item">
          <span className={`insights-swatch insights-swatch--${String(s.swatch)}`} aria-hidden />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

export function HeadlinePanel({ stats }: { stats: FraudStats }) {
  const period = countOf(stats.window_days, "day");
  return (
    <section className="card" aria-labelledby="fd-headline">
      <h2 id="fd-headline">Last {period} at a glance</h2>
      <dl className="metric-grid">
        <Metric
          label="Payments scored"
          value={stats.scored}
          definition="Transfers and bill payments the fraud engine decided."
        />
        <Metric
          label="Hold rate"
          value={formatRate(stats.hold_rate)}
          definition={`${countOf(stats.totals.REVIEW ?? 0, "payment")} held for an analyst.`}
          tone={stats.hold_rate > 0 ? "warning" : undefined}
        />
        <Metric
          label="Block rate"
          value={formatRate(stats.block_rate)}
          definition={`${countOf(stats.totals.BLOCK ?? 0, "payment")} refused outright.`}
          tone={stats.block_rate > 0 ? "danger" : undefined}
        />
        <Metric
          label="Extra verification"
          value={stats.totals.STEP_UP ?? 0}
          definition="Payments that asked the customer for an authenticator code."
        />
        <Metric
          label="Rules-only decisions"
          value={stats.model_fallbacks}
          definition="The model was unavailable, so the rules decided alone (fail safe)."
          tone={stats.model_fallbacks > 0 ? "warning" : undefined}
        />
        <Metric
          label="Scoring time (p95)"
          value={stats.latency_p95_ms === null ? "-" : `${stats.latency_p95_ms.toFixed(1)} ms`}
          definition="95% of decisions took at most this long; the target is under 100 ms."
        />
      </dl>
    </section>
  );
}

function DecisionsTable({ rows }: { rows: DayRow[] }) {
  return (
    <details>
      <summary>Decisions per day (table)</summary>
      <div className="entries">
        <table>
          <caption className="sr-only">Fraud decisions per UTC day, oldest first</caption>
          <thead>
            <tr>
              <th scope="col">Day (UTC)</th>
              {DECISION_SERIES.map((s) => (
                <th scope="col" className="right" key={s.key}>
                  {s.label}
                </th>
              ))}
              <th scope="col" className="right">
                Hold rate
              </th>
              <th scope="col" className="right">
                Block rate
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.day}>
                <th scope="row">{r.day}</th>
                {DECISION_SERIES.map((s) => (
                  <td className="right" key={s.key}>
                    {r[s.key]}
                  </td>
                ))}
                <td className="right">{r.holdPct === null ? "-" : `${r.holdPct.toFixed(1)}%`}</td>
                <td className="right">{r.blockPct === null ? "-" : `${r.blockPct.toFixed(1)}%`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export function DecisionsPanel({ stats }: { stats: FraudStats }) {
  const rows = dailyRows(stats);
  const busiest = rows.reduce<DayRow | null>(
    (best, r) => (best === null || r.scored > best.scored ? r : best),
    null,
  );
  const summary =
    stats.scored === 0
      ? "No payments were scored in this period."
      : `${countOf(stats.scored, "payment")} scored over ${countOf(rows.length, "day")}: ` +
        DECISION_SERIES.map((s) => `${s.label} ${String(stats.totals[s.key] ?? 0)}`).join(", ") +
        (busiest ? `. Busiest day ${busiest.day} with ${String(busiest.scored)}.` : ".");
  const rateSummary = `Hold rate ${formatRate(stats.hold_rate)} and block rate ${formatRate(stats.block_rate)} over the period; daily values are in the table.`;
  return (
    <section className="card" aria-labelledby="fd-decisions">
      <h2 id="fd-decisions">Decisions over time</h2>
      <p className="muted">{summary}</p>
      {stats.scored === 0 ? (
        <EmptyState message="No fraud decisions in this period." />
      ) : (
        <>
          <Legend />
          <figure className="insights-chart insights-chart--bars" aria-label={summary}>
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart
                data={rows}
                margin={{ top: 10, right: 16, left: 0, bottom: 10 }}
                title="Fraud decisions per day"
                desc={summary}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} />
                {DECISION_SERIES.map((s) => (
                  <Bar
                    key={s.key}
                    dataKey={s.key}
                    name={s.label}
                    stackId="decisions"
                    fill={s.color}
                    stroke="var(--surface)"
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </figure>
          <h3>Hold and block rates per day</h3>
          <p className="muted">
            Held for review: solid line. Blocked: dashed line. Days without payments have no point.
          </p>
          <figure className="insights-chart insights-chart--bars" aria-label={rateSummary}>
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <LineChart
                data={rows}
                margin={{ top: 10, right: 16, left: 0, bottom: 10 }}
                title="Hold and block rates per day"
                desc={rateSummary}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} unit="%" />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [`${String(v)}%`]} />
                <Line
                  dataKey="holdPct"
                  name="Hold rate"
                  stroke="var(--chart-4)"
                  strokeWidth={2}
                  dot
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Line
                  dataKey="blockPct"
                  name="Block rate"
                  stroke="var(--chart-5)"
                  strokeWidth={2}
                  strokeDasharray="6 4"
                  dot
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </figure>
          <DecisionsTable rows={rows} />
        </>
      )}
    </section>
  );
}

export function RiskBandsPanel({ stats }: { stats: FraudStats }) {
  const rows = stats.risk_bands.map((b) => ({ band: bandLabel(b), count: b.count }));
  const summary = `Payment decisions per risk band: ${rows.map((r) => `${r.band} ${String(r.count)}`).join(", ")}.`;
  return (
    <section className="card" aria-labelledby="fd-bands">
      <h2 id="fd-bands">Risk score distribution</h2>
      <p className="muted">
        Every payment gets a risk score from 0 to 100. From 30 the customer is asked for an
        authenticator code, from 60 the payment is held for an analyst, and from 85 it is blocked.
      </p>
      {stats.scored === 0 ? (
        <EmptyState message="No payments were scored in this period." />
      ) : (
        <>
          <figure className="insights-chart insights-chart--bars" aria-label={summary}>
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart
                data={rows}
                margin={{ top: 10, right: 16, left: 0, bottom: 10 }}
                title="Payments per risk band"
                desc={summary}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="band" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Payments"]} />
                <Bar
                  dataKey="count"
                  name="Payments"
                  fill="var(--chart-1)"
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </figure>
          <details>
            <summary>Payments per risk band (table)</summary>
            <div className="entries">
              <table>
                <caption className="sr-only">Payment decisions per risk-score band</caption>
                <thead>
                  <tr>
                    <th scope="col">Risk score</th>
                    <th scope="col" className="right">
                      Payments
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.band}>
                      <th scope="row">{r.band}</th>
                      <td className="right">{r.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </section>
  );
}

export function TopReasonsPanel({ stats }: { stats: FraudStats }) {
  const rows = stats.top_rules.map((r) => ({ ...r, label: reasonLabel(r.code) }));
  const summary = `Most frequent reasons: ${rows.map((r) => `${r.label} ${String(r.count)}`).join(", ")}.`;
  return (
    <section className="card" aria-labelledby="fd-reasons">
      <h2 id="fd-reasons">Top reasons</h2>
      <p className="muted">
        How often each rule, model or unusual-pattern reason contributed to a decision. One payment
        can have several reasons.
      </p>
      {rows.length === 0 ? (
        <EmptyState message="No reasons were recorded in this period." />
      ) : (
        <>
          <figure className="insights-chart fraud-dash-reasons" aria-label={summary}>
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 300 }}
            >
              <BarChart
                data={rows}
                layout="vertical"
                title="Top fraud reasons"
                desc={summary}
                margin={{ top: 4, right: 24, left: 8, bottom: 4 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
                <XAxis type="number" stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="label"
                  stroke="var(--muted)"
                  fontSize={11}
                  width={170}
                />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Decisions"]} />
                <Bar
                  dataKey="count"
                  name="Decisions"
                  fill="var(--chart-2)"
                  radius={[0, 4, 4, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </figure>
          <div className="entries">
            <table>
              <caption className="sr-only">Reason codes, most frequent first</caption>
              <thead>
                <tr>
                  <th scope="col">Reason</th>
                  <th scope="col">Code</th>
                  <th scope="col">Source</th>
                  <th scope="col" className="right">
                    Decisions
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.code}-${r.source ?? ""}`}>
                    <th scope="row">{r.label}</th>
                    <td>
                      <code>{r.code}</code>
                    </td>
                    <td>{sourceLabel(r.source)}</td>
                    <td className="right">{r.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

export function AlertsPanel({ stats }: { stats: FraudStats }) {
  const { OPEN, CONFIRMED_FRAUD, FALSE_POSITIVE } = stats.alerts;
  return (
    <section className="card" aria-labelledby="fd-alerts">
      <h2 id="fd-alerts">Alerts raised in the period</h2>
      <p className="muted">Where the alerts raised in the period stand now.</p>
      <dl className="metric-grid">
        <Metric
          label="Open"
          value={OPEN}
          definition="Waiting for an analyst to release or reject."
          tone={OPEN > 0 ? "warning" : undefined}
        />
        <Metric
          label="Confirmed fraud"
          value={CONFIRMED_FRAUD}
          definition="Rejected by an analyst; the money was not sent."
        />
        <Metric
          label="Released"
          value={FALSE_POSITIVE}
          definition="Judged genuine by an analyst; the payment went ahead."
        />
      </dl>
      <p>
        <Link to="/staff/fraud">Open the alert queue</Link>
      </p>
    </section>
  );
}

export function RiskyPayeesPanel({ stats }: { stats: FraudStats }) {
  return (
    <section className="card" aria-labelledby="fd-payees">
      <h2 id="fd-payees">Destinations behind flagged payments</h2>
      <p className="muted">
        Receiving accounts of payments scored 60 or more, across customers. Several customers paying
        one account can mean a money mule. Account numbers are masked.
      </p>
      {stats.risky_payees.length === 0 ? (
        <EmptyState message="No payments to a destination were held or blocked in this period." />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">Destination accounts, most flagged first</caption>
            <thead>
              <tr>
                <th scope="col">Bank</th>
                <th scope="col">Account</th>
                <th scope="col" className="right">
                  Flagged payments
                </th>
                <th scope="col" className="right">
                  Customers
                </th>
                <th scope="col" className="right">
                  Highest risk
                </th>
              </tr>
            </thead>
            <tbody>
              {stats.risky_payees.map((p) => (
                <tr key={`${p.bank_code}-${p.account}`}>
                  <td>{p.bank_code}</td>
                  <td>{p.account}</td>
                  <td className="right">{p.flagged_transfers}</td>
                  <td className="right">{p.customers}</td>
                  <td className="right">{p.max_risk}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** Section 5 Fraud Monitoring Dashboard: decisions over time, hold and block rates, risk scores,
 * top reasons, alert outcomes and risky destinations (GET /fraud/stats). Aggregates only; the
 * alert queue and Security monitoring hold the per-payment detail. */
export default function FraudDashboard() {
  const me = useCurrentUser();
  const allowed = canReviewFraud(me);
  const [days, setDays] = useState<"7" | "30" | "90">("7");
  const query = useQuery({
    queryKey: ["fraud", "stats", days],
    queryFn: () => api.fraudStats(Number(days)),
    enabled: allowed,
    placeholderData: keepPreviousData,
    refetchInterval: REFRESH_MS,
  });
  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Fraud dashboard" />
        <Alert tone="error">
          Access restricted: the fraud dashboard needs the Fraud Analyst or Administrator role.
        </Alert>
      </div>
    );
  }
  const data = query.data;
  return (
    <div className="page dashboard-page">
      <PageHeader
        title="Fraud dashboard"
        description="How the fraud engine decided payments: trends, risk scores, reasons and alert outcomes. Aggregates only."
        actions={
          data && (
            <span className="muted" title={formatDateTime(data.generated_at)}>
              Updated {formatRelative(data.generated_at)}
            </span>
          )
        }
      />
      <section className="rm-filters" aria-label="Filter the dashboard">
        <FilterSelect label="Period" value={days} onChange={setDays} options={PERIOD_OPTIONS} />
      </section>
      {query.isPending && <LoadingState label="Loading the fraud dashboard..." />}
      {query.isError && (
        <ErrorState
          message={errorMessage(query.error, "Could not load the fraud dashboard.")}
          onRetry={() => {
            void query.refetch();
          }}
        />
      )}
      {data && (
        <>
          <HeadlinePanel stats={data} />
          <DecisionsPanel stats={data} />
          <RiskBandsPanel stats={data} />
          <TopReasonsPanel stats={data} />
          <AlertsPanel stats={data} />
          <RiskyPayeesPanel stats={data} />
        </>
      )}
    </div>
  );
}
