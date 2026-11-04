import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../api/client";
import type { BehaviourAnalytics as Analytics, BehaviourSegment } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { countOf, formatDateTime, formatRelative } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { TOOLTIP_STYLE, formatShare } from "../insightsChart";
import { FilterSelect, Metric } from "./dashboardParts";
import { SEGMENT_LABEL, canAccessRMInsights, segmentLabel } from "./rmInsightsFormat";
import "./dashboards.css";

/** Typed by the OpenAPI union, so a segment the API adds fails the type check. */
const SEGMENTS: readonly BehaviourSegment[] = [
  "HIGH_NET_WORTH",
  "BUDGET_CONSCIOUS",
  "FREQUENT_TRANSACTOR",
  "DISCRETIONARY_SPENDER",
  "INACTIVE_SAVER",
  "UNSEGMENTED",
];

const SEGMENT_OPTIONS = [
  { value: "", label: "All segments" },
  ...SEGMENTS.map((s) => ({ value: s, label: SEGMENT_LABEL[s] ?? segmentLabel(s) })),
] as const;

/** Clickstream events are kept for 180 days (TASK-055), the longest period offered. */
const PERIODS = [
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "180", label: "Last 180 days" },
] as const;

export const PRODUCT_LABEL: Record<string, string> = {
  SAVINGS_ACCOUNT: "Savings account",
  CURRENT_ACCOUNT: "Current account",
  PERSONAL_LOAN: "Personal loan",
  AUTO_LOAN: "Auto loan",
  HOME_LOAN: "Home loan",
};

const EVENT_LABEL: Record<string, string> = {
  page_view: "Page views",
  loan_calculator_used: "Loan calculator uses",
  transfer_initiated: "Transfers started",
  account_viewed: "Account views",
  product_clicked: "Product clicks",
  search_performed: "Searches",
};

const CATEGORY_LABEL: Record<string, string> = {
  LOAN: "Loans and credit",
  SAVINGS: "Savings",
  INVESTMENT: "Investments",
  INSURANCE: "Insurance",
};

const TYPE_LABEL: Record<string, string> = {
  TRANSFER: "Transfers",
  BILL_PAYMENT: "Bill payments",
  LOAN_REPAYMENT: "Loan repayments",
};

/** Exact session length ("50 s", "2 min 5 s"): averages of a few minutes need the seconds. */
export function sessionLength(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes === 0 ? `${String(rest)} s` : `${String(minutes)} min ${String(rest)} s`;
}

function label(map: Record<string, string>, key: string): string {
  return map[key] ?? segmentLabel(key);
}

export function SegmentsPanel({
  panel,
  canDrill,
}: {
  panel: Analytics["segments"];
  canDrill: boolean;
}) {
  const rows = panel.items.map((i) => ({ name: segmentLabel(i.segment), customers: i.customers }));
  return (
    <section className="card" aria-labelledby="ba-segments">
      <h2 id="ba-segments">Segment mix</h2>
      <p className="muted">
        Every customer's latest weekly segment (K-Means on recency, frequency, spend and lifestyle
        ratios); customers the job has not seen yet are "Not segmented yet". Not filtered, so the
        whole mix stays visible.{" "}
        {panel.last_segmented_at
          ? `Last segmentation run ${formatRelative(panel.last_segmented_at)}.`
          : "The segmentation job has not run yet."}
      </p>
      {panel.total_customers === 0 ? (
        <EmptyState message="There are no customers yet." />
      ) : (
        <>
          <div
            role="img"
            aria-label={`Customers per segment: ${panel.items.map((i) => `${segmentLabel(i.segment)} ${String(i.customers)}`).join(", ")}.`}
            className="insights-chart insights-chart--bars"
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart data={rows} margin={{ top: 10, right: 16, left: 0, bottom: 40 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="name"
                  stroke="var(--muted)"
                  fontSize={11}
                  interval={0}
                  angle={-20}
                  textAnchor="end"
                />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Customers"]} />
                <Bar
                  dataKey="customers"
                  fill="var(--chart-1)"
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="entries">
            <table>
              <caption className="sr-only">Customers and average features per segment</caption>
              <thead>
                <tr>
                  <th scope="col">Segment</th>
                  <th scope="col" className="right">
                    Customers
                  </th>
                  <th scope="col" className="right">
                    Share
                  </th>
                  <th scope="col" className="right" title="Average monthly spend the job measured">
                    Avg monthly spend
                  </th>
                  <th scope="col" className="right" title="Average transactions per 30 days">
                    Avg transactions / month
                  </th>
                  <th scope="col" className="right" title="Net savings divided by inflows">
                    Avg savings ratio
                  </th>
                  {canDrill && <th scope="col">Customers</th>}
                </tr>
              </thead>
              <tbody>
                {panel.items.map((i) => (
                  <tr key={i.segment}>
                    <th scope="row">{segmentLabel(i.segment)}</th>
                    <td className="right">{i.customers}</td>
                    <td className="right">{formatShare(i.share_pct)}</td>
                    <td className="right">
                      {i.avg_monthly_spend_minor === null ? (
                        <span className="muted">—</span>
                      ) : (
                        <Amount minor={i.avg_monthly_spend_minor} currency="INR" />
                      )}
                    </td>
                    <td className="right">{i.avg_monthly_transactions ?? "—"}</td>
                    <td className="right">{i.avg_savings_ratio ?? "—"}</td>
                    {canDrill && (
                      <td>
                        {i.customers > 0 ? (
                          <Link to={`/staff/insights?segment=${i.segment}`}>
                            View {countOf(i.customers, "customer")}
                          </Link>
                        ) : (
                          <span className="muted">None</span>
                        )}
                      </td>
                    )}
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

export function SpendingPanel({
  rows,
  currency,
  period,
}: {
  rows: Analytics["spending"];
  currency: string;
  period: string;
}) {
  return (
    <section className="card" aria-labelledby="ba-spending">
      <h2 id="ba-spending">Spending patterns</h2>
      <p className="muted">
        Money customers paid out in the {period}: posted debits from accounts they hold as primary
        holder, excluding reversals and moves between their own accounts. Average spend is per
        customer who spent at all.
      </p>
      {rows.every((r) => r.transactions === 0) ? (
        <EmptyState message={`No customer spending in the ${period}.`} />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">Spending per segment</caption>
            <thead>
              <tr>
                <th scope="col">Segment</th>
                <th scope="col" className="right">
                  Spenders
                </th>
                <th scope="col" className="right">
                  Total spend
                </th>
                <th scope="col" className="right">
                  Payments
                </th>
                <th scope="col" className="right">
                  Avg per spender
                </th>
                <th scope="col">Mix</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.segment}>
                  <th scope="row">{segmentLabel(r.segment)}</th>
                  <td className="right">
                    {r.spenders} <span className="muted">of {r.customers}</span>
                  </td>
                  <td className="right">
                    <Amount minor={r.spend_minor} currency={currency} />
                  </td>
                  <td className="right">{r.transactions}</td>
                  <td className="right">
                    <Amount minor={r.avg_spend_per_spender_minor} currency={currency} />
                  </td>
                  <td>
                    {r.by_type.length === 0 ? (
                      <span className="muted">—</span>
                    ) : (
                      r.by_type
                        .map(
                          (t) =>
                            `${label(TYPE_LABEL, t.transaction_type)}: ${String(t.transactions)}`,
                        )
                        .join(", ")
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

export function AffinityPanel({ panel, period }: { panel: Analytics["affinity"]; period: string }) {
  return (
    <section className="card" aria-labelledby="ba-affinity">
      <h2 id="ba-affinity">Product affinity</h2>
      <p className="muted">
        Share of each segment's customers holding a product today (an active account as holder or
        joint holder, an active loan), and clicks on recommended products in the {period}.
      </p>
      {panel.segments.length === 0 ? (
        <EmptyState message="No customers in this selection." />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">Product holdings by segment</caption>
            <thead>
              <tr>
                <th scope="col">Segment</th>
                {panel.products.map((p) => (
                  <th key={p} scope="col" className="right">
                    {label(PRODUCT_LABEL, p)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {panel.segments.map((s) => (
                <tr key={s.segment}>
                  <th scope="row">
                    {segmentLabel(s.segment)} <span className="muted">({s.customers})</span>
                  </th>
                  {s.holdings.map((h) => (
                    <td key={h.product} className="right">
                      {formatShare(h.share_pct)} <span className="muted">({h.holders})</span>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>Recommendation clicks</h3>
      {panel.recommendation_clicks.length === 0 ? (
        <EmptyState message={`No clicks on recommended products in the ${period}.`} />
      ) : (
        <div className="entries">
          <table>
            <caption className="sr-only">
              Clicks on recommended products, most clicked first
            </caption>
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col" className="right">
                  Clicks
                </th>
                <th scope="col" className="right">
                  Customers
                </th>
              </tr>
            </thead>
            <tbody>
              {panel.recommendation_clicks.map((c) => (
                <tr key={c.product_id}>
                  <th scope="row">{c.title}</th>
                  <td className="right">{c.clicks}</td>
                  <td className="right">{c.customers}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <InterestByCategory panel={panel} />
    </section>
  );
}

/** ADR-0057: product interest across the behavioural profiles of customers who turned usage
 * analytics on (the daily job's own 90-day window, not the period filter). */
export function InterestByCategory({ panel }: { panel: Analytics["affinity"] }) {
  const interests = panel.interests ?? [];
  const data = interests.map((i) => ({
    category: label(CATEGORY_LABEL, i.category),
    customers: i.customers,
  }));
  return (
    <>
      <h3>Product interest by category</h3>
      <p className="muted">
        From the behavioural profiles of {countOf(panel.profiled_customers, "customer")} who turned
        usage analytics on: card opens, loan calculator uses and product page visits in the last 90
        days, recent ones weighing more. Refreshed daily.
      </p>
      {interests.length === 0 ? (
        <EmptyState message="No product interest recorded yet." />
      ) : (
        <>
          <div
            role="img"
            aria-label={`Customers interested per product category: ${data.map((d) => `${d.category} ${String(d.customers)}`).join(", ")}.`}
            className="insights-chart insights-chart--bars"
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart data={data} margin={{ top: 10, right: 16, left: 0, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="category" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Customers"]} />
                <Bar
                  dataKey="customers"
                  fill="var(--chart-2)"
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="entries">
            <table>
              <caption className="sr-only">
                Product interest by category, most customers first
              </caption>
              <thead>
                <tr>
                  <th scope="col">Category</th>
                  <th scope="col" className="right">
                    Customers
                  </th>
                  <th scope="col" className="right">
                    Share of profiled
                  </th>
                  <th scope="col" className="right">
                    Average score
                  </th>
                  <th scope="col" className="right">
                    Signals
                  </th>
                </tr>
              </thead>
              <tbody>
                {interests.map((i) => (
                  <tr key={i.category}>
                    <th scope="row">{label(CATEGORY_LABEL, i.category)}</th>
                    <td className="right">{i.customers}</td>
                    <td className="right">{formatShare(i.share_pct)}</td>
                    <td className="right">{i.avg_score.toFixed(2)}</td>
                    <td className="right">{i.signals}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

export function EngagementPanel({
  panel,
  period,
}: {
  panel: Analytics["engagement"];
  period: string;
}) {
  const daily = panel.daily.map((d) => ({
    day: d.day.slice(5),
    active: d.active_customers,
    sessions: d.sessions,
  }));
  return (
    <section className="card" aria-labelledby="ba-engagement">
      <h2 id="ba-engagement">Engagement</h2>
      <p className="muted">
        From app clickstream events in the {period}, of the customers who turned usage analytics on.
        A session is one browser visit of one customer, ended by 30 minutes without activity; its
        length is the time from its first to its last event.
      </p>
      <dl className="metric-grid">
        <Metric
          label="Active customers"
          value={`${String(panel.active_customers)} of ${String(panel.customers)}`}
          definition={`Customers with at least one event (${formatShare(panel.active_share_pct)}).`}
        />
        <Metric
          label="Customers sharing analytics"
          value={`${String(panel.consenting_customers)} of ${String(panel.customers)}`}
          definition="Customers who turned usage analytics on; only their activity is recorded."
        />
        <Metric
          label="Sessions"
          value={panel.sessions}
          definition="Customer visits, split after 30 minutes without activity."
        />
        <Metric
          label="Events per session"
          value={panel.events_per_session}
          definition={`${countOf(panel.events, "event")} in total.`}
        />
        <Metric
          label="Average session length"
          value={panel.sessions ? sessionLength(panel.avg_session_seconds) : "—"}
          definition="Mean of last minus first event time; a one-event session counts as 0 seconds."
        />
      </dl>
      {panel.events === 0 ? (
        <EmptyState
          message={`No clickstream events in the ${period}. Engagement appears once the app records events.`}
        />
      ) : (
        <>
          <div
            role="img"
            aria-label={`Active customers per day: ${panel.daily.map((d) => `${d.day} ${String(d.active_customers)}`).join(", ")}.`}
            className="insights-chart insights-chart--bars"
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart data={daily} margin={{ top: 10, right: 16, left: 0, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Active customers"]} />
                <Bar
                  dataKey="active"
                  fill="var(--chart-1)"
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div
            role="img"
            aria-label={`Sessions per day: ${panel.daily.map((d) => `${d.day} ${String(d.sessions)}`).join(", ")}.`}
            className="insights-chart insights-chart--bars"
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 500, height: 240 }}
            >
              <BarChart data={daily} margin={{ top: 10, right: 16, left: 0, bottom: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={12} allowDecimals={false} />
                <Tooltip {...TOOLTIP_STYLE} formatter={(v) => [String(v), "Sessions"]} />
                <Bar
                  dataKey="sessions"
                  fill="var(--chart-3)"
                  radius={[4, 4, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <details>
            <summary>Active customers and sessions per day (table)</summary>
            <div className="entries">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Day (UTC)</th>
                    <th scope="col" className="right">
                      Active customers
                    </th>
                    <th scope="col" className="right">
                      Events
                    </th>
                    <th scope="col" className="right">
                      Sessions
                    </th>
                    <th scope="col" className="right">
                      Average session
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {panel.daily.map((d) => (
                    <tr key={d.day}>
                      <th scope="row">{d.day}</th>
                      <td className="right">{d.active_customers}</td>
                      <td className="right">{d.events}</td>
                      <td className="right">{d.sessions}</td>
                      <td className="right">
                        {d.sessions ? sessionLength(d.avg_session_seconds) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <div className="entries">
            <table>
              <caption className="sr-only">Events by type and active customers by segment</caption>
              <thead>
                <tr>
                  <th scope="col">Event</th>
                  <th scope="col" className="right">
                    Count
                  </th>
                </tr>
              </thead>
              <tbody>
                {panel.by_event_type.map((e) => (
                  <tr key={e.event_type}>
                    <th scope="row">{label(EVENT_LABEL, e.event_type)}</th>
                    <td className="right">{e.events}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="entries">
            <table>
              <caption className="sr-only">Active customers by segment</caption>
              <thead>
                <tr>
                  <th scope="col">Segment</th>
                  <th scope="col" className="right">
                    Active customers
                  </th>
                  <th scope="col" className="right">
                    Share active
                  </th>
                </tr>
              </thead>
              <tbody>
                {panel.by_segment.map((s) => (
                  <tr key={s.segment}>
                    <th scope="row">{segmentLabel(s.segment)}</th>
                    <td className="right">
                      {s.active_customers} <span className="muted">of {s.customers}</span>
                    </td>
                    <td className="right">{formatShare(s.active_share_pct)}</td>
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

/** TASK-101 (UC-12 Behavioural Analytics): segment mix, spending, product affinity and engagement
 * across all customers (GET /insights/behaviour). Aggregates only; the per-customer view is the
 * RM insights page, linked per segment for roles that may open it. */
export default function BehaviourAnalytics() {
  const me = useCurrentUser();
  const allowed = canAccessRMInsights(me);
  const [days, setDays] = useState<"30" | "90" | "180">("30");
  const [segment, setSegment] = useState<BehaviourSegment | "">("");
  const query = useQuery({
    queryKey: ["insights", "behaviour", days, segment],
    queryFn: () => api.behaviourAnalytics(Number(days), segment || undefined),
    enabled: allowed,
    placeholderData: keepPreviousData,
  });
  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Behavioural analytics" />
        <Alert tone="error">
          Access restricted: behavioural analytics need the Relationship Manager or Administrator
          role.
        </Alert>
      </div>
    );
  }
  const data = query.data;
  const period = `last ${countOf(Number(days), "day")}`;
  return (
    <div className="page dashboard-page">
      <PageHeader
        title="Behavioural analytics"
        description="How customer segments spend, which products they hold and click, and how they use the app. Aggregates only."
        actions={
          data && (
            <span className="muted" title={formatDateTime(data.generated_at)}>
              Updated {formatRelative(data.generated_at)}
            </span>
          )
        }
      />
      <section className="rm-filters" aria-label="Filter analytics">
        <FilterSelect label="Period" value={days} onChange={setDays} options={PERIODS} />
        <FilterSelect
          label="Segment"
          value={segment}
          onChange={setSegment}
          options={SEGMENT_OPTIONS}
        />
      </section>
      {query.isPending && <LoadingState label="Loading behavioural analytics..." />}
      {query.isError && (
        <ErrorState
          message={errorMessage(query.error, "Could not load behavioural analytics.")}
          onRetry={() => {
            void query.refetch();
          }}
        />
      )}
      {data && (
        <>
          <SegmentsPanel panel={data.segments} canDrill={canAccessRMInsights(me)} />
          <SpendingPanel rows={data.spending} currency={data.currency} period={period} />
          <AffinityPanel panel={data.affinity} period={period} />
          <EngagementPanel panel={data.engagement} period={period} />
        </>
      )}
    </div>
  );
}
