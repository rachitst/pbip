import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import type {
  AccountOut,
  CategoryShiftItem,
  FinancialHealthScoreResponse,
  HealthScoreComponent,
  SpendingBreakdownResponse,
  SpendingTrendsResponse,
} from "../api/client";
import { ASSESSMENTS, RATINGS, categoryLabel, paymentCount } from "../lib/analytics";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import { formatMinor } from "../lib/money";
import { Alert } from "../ui/Alert";
import { Amount } from "../ui/Amount";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { TOOLTIP_STYLE, categoryTooltip, donutSlices, formatShare } from "./insightsChart";

const TITLE = "Spending & Financial Health";

export function Insights() {
  const accountsQuery = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const accounts = accountsQuery.data ?? [];
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);

  const activeAccountId = selectedAccountId ?? accounts[0]?.id ?? null;

  const spendingQuery = useQuery({
    queryKey: ["analytics", "spending", activeAccountId],
    queryFn: () => {
      if (!activeAccountId) throw new Error("No active account selected");
      return api.spendingBreakdown(activeAccountId);
    },
    enabled: Boolean(activeAccountId),
  });

  const trendsQuery = useQuery({
    queryKey: ["analytics", "trends", activeAccountId],
    queryFn: () => {
      if (!activeAccountId) throw new Error("No active account selected");
      return api.spendingTrends(activeAccountId);
    },
    enabled: Boolean(activeAccountId),
  });

  const healthQuery = useQuery({
    queryKey: ["analytics", "health-score", activeAccountId],
    queryFn: () => {
      if (!activeAccountId) throw new Error("No active account selected");
      return api.financialHealthScore(activeAccountId);
    },
    enabled: Boolean(activeAccountId),
  });

  if (accountsQuery.isPending) {
    return (
      <div className="page">
        <PageHeader title={TITLE} />
        <LoadingState label="Loading your accounts..." />
      </div>
    );
  }

  if (accountsQuery.isError) {
    return (
      <div className="page">
        <PageHeader title={TITLE} />
        <ErrorState
          message={errorMessage(accountsQuery.error, "Could not load accounts.")}
          onRetry={() => {
            void accountsQuery.refetch();
          }}
        />
      </div>
    );
  }

  if (accounts.length === 0) {
    return (
      <div className="page">
        <PageHeader title={TITLE} />
        <EmptyState message="You have no accounts yet." />
      </div>
    );
  }

  const isPending = spendingQuery.isPending || trendsQuery.isPending || healthQuery.isPending;
  const isError = spendingQuery.isError || trendsQuery.isError || healthQuery.isError;
  const firstError = spendingQuery.error ?? trendsQuery.error ?? healthQuery.error;

  const handleRetryAll = () => {
    void spendingQuery.refetch();
    void trendsQuery.refetch();
    void healthQuery.refetch();
  };

  return (
    <div className="page">
      <PageHeader
        title={TITLE}
        actions={
          accounts.length > 1 ? (
            <div className="insights-account">
              <label htmlFor="account-selector" className="insights-account__label">
                Account:
              </label>
              <select
                id="account-selector"
                value={activeAccountId ?? ""}
                onChange={(e) => {
                  setSelectedAccountId(e.target.value);
                }}
              >
                {accounts.map((acc: AccountOut) => (
                  <option key={acc.id} value={acc.id}>
                    {accountTypeLabel(acc.account_type)} ({acc.account_number_masked})
                  </option>
                ))}
              </select>
            </div>
          ) : undefined
        }
      />

      {isPending && <LoadingState label="Loading spending & financial health insights..." />}

      {isError && (
        <ErrorState
          message={errorMessage(firstError, "Could not load financial analytics.")}
          onRetry={handleRetryAll}
        />
      )}

      {!isPending && !isError && (
        <div className="insights">
          <FinancialHealthSection health={healthQuery.data} />
          <CategorySpendingSection spending={spendingQuery.data} />
          <SpendingTrendsSection trends={trendsQuery.data} />
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------------
// Financial Health Score Section
// -----------------------------------------------------------------------------

function FinancialHealthSection({ health }: { health: FinancialHealthScoreResponse }) {
  const rating = RATINGS[health.rating];
  const recommendations = health.recommendations ?? [];

  return (
    <section aria-labelledby="health-score-heading" className="insights-section">
      <div className="insights-section__header">
        <div>
          <h2 id="health-score-heading">Financial Health Score</h2>
          <p className="muted insights-section__summary">
            Computed from {health.months_observed} observed{" "}
            {health.months_observed === 1 ? "month" : "months"} of banking history.
          </p>
        </div>
        <div className="insights-score">
          <span className="insights-score__value">
            {health.overall_score}
            <span className="insights-score__max"> / 100</span>
          </span>
          <span className={`badge badge--${rating.tone}`}>{rating.label}</span>
        </div>
      </div>

      {health.insufficient_data && (
        <Alert tone="info">
          Historical data is currently accumulating ({health.months_observed} of{" "}
          {health.window_months} months observed). Component metrics will gain confidence as
          additional monthly cycles complete.
        </Alert>
      )}

      <div className="insights-components">
        <HealthComponent
          name="Savings"
          component={health.components.savings_rate}
          metricLabel="Savings Rate"
          ariaLabel="Savings score"
        />
        <HealthComponent
          name="Buffer"
          component={health.components.buffer_months}
          metricLabel="Cash Buffer"
          ariaLabel="Cash buffer score"
        />
        <HealthComponent
          name="Debt"
          component={health.components.emi_to_income}
          metricLabel="Loan / EMI Ratio"
          ariaLabel="Debt repayments score"
        />
        <HealthComponent
          name="Expense Volatility"
          component={health.components.expense_volatility}
          metricLabel="Spending variability"
          ariaLabel="Expense volatility score"
        />
      </div>

      {recommendations.length > 0 && (
        <div className="insights-recommendations">
          <h3 className="insights-eyebrow">Actionable Financial Insights</h3>
          <ul>
            {recommendations.map((rec, i) => (
              <li key={i}>{rec}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

interface HealthComponentProps {
  name: string;
  component: HealthScoreComponent;
  metricLabel: string;
  ariaLabel: string;
}

function HealthComponent({ name, component, metricLabel, ariaLabel }: HealthComponentProps) {
  const assessment = ASSESSMENTS[component.assessment];
  return (
    <div className="insights-component">
      <div className="row">
        <span className="insights-component__name">{name}</span>
        <span className="insights-num">
          {component.score} / {component.max_score} pts
        </span>
      </div>
      <progress
        className={`meter meter--${assessment.tone}`}
        value={component.score}
        max={component.max_score}
        aria-label={ariaLabel}
      />
      <div className="insights-component__metric">
        <span>
          {metricLabel}: <strong>{component.formatted_value}</strong>
        </span>
        <span className={`badge badge--${assessment.tone}`}>{assessment.label}</span>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Category Spending Donut Chart Section
// -----------------------------------------------------------------------------

function CategorySpendingSection({ spending }: { spending: SpendingBreakdownResponse }) {
  const categories = spending.categories ?? [];
  const hasCategories = categories.length > 0 && spending.total_spend_minor > 0;
  const slices = donutSlices(categories);
  const swatch = (category: string) =>
    slices.find((slice) => slice.categories.includes(category))?.colorIndex ?? slices.length;

  const summaryText = hasCategories
    ? `Total spending of ${formatMinor(spending.total_spend_minor, spending.currency)} across ${categories.length} categories in ${spending.period}.`
    : `No spending recorded for ${spending.period}.`;

  return (
    <section aria-labelledby="category-spending-heading" className="insights-section">
      <div className="insights-section__header">
        <div>
          <h2 id="category-spending-heading">Spending by Category</h2>
          <p className="muted insights-section__summary">{summaryText}</p>
        </div>
        <div className="insights-total">
          <span className="muted insights-total__period">Period: {spending.period}</span>
          <div className="insights-total__value">
            <Amount minor={spending.total_spend_minor} currency={spending.currency} />
          </div>
        </div>
      </div>

      {!hasCategories ? (
        <EmptyState message="No categorized debit transactions recorded for this period." />
      ) : (
        <>
          <div className="insights-breakdown">
            <div
              role="region"
              aria-label="Category spending distribution donut chart"
              className="insights-chart insights-chart--donut"
            >
              <ResponsiveContainer
                width="100%"
                height="100%"
                minWidth={0}
                initialDimension={{ width: 320, height: 260 }}
              >
                <PieChart title={`Spending by category, ${spending.period}`} desc={summaryText}>
                  <Tooltip
                    {...TOOLTIP_STYLE}
                    formatter={categoryTooltip(slices, spending.currency)}
                  />
                  <Pie
                    rootTabIndex={-1}
                    data={slices}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={60}
                    outerRadius={90}
                    paddingAngle={2}
                    isAnimationActive={false}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>

            <ul className="insights-legend" aria-label="Spending by category">
              {categories.map((cat) => (
                <li key={cat.category} className="insights-legend__item">
                  <span className="insights-legend__name">
                    <span
                      aria-hidden="true"
                      className={`insights-swatch insights-swatch--${swatch(cat.category)}`}
                    />
                    <span className="insights-legend__label">{categoryLabel(cat.category)}</span>
                    <span className="muted insights-legend__count">
                      {paymentCount(cat.transaction_count)}
                    </span>
                  </span>
                  <span className="insights-legend__figures">
                    <strong>
                      <Amount minor={cat.amount_minor} currency={spending.currency} />
                    </strong>
                    <span className="badge badge--neutral insights-num">
                      {formatShare(cat.percentage)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <details className="insights-fallback">
            <summary>View accessible category table</summary>
            <div className="entries">
              <table aria-label="Category spending breakdown table">
                <thead>
                  <tr>
                    <th scope="col">Category</th>
                    <th scope="col" className="right">
                      Transactions
                    </th>
                    <th scope="col" className="right">
                      Amount
                    </th>
                    <th scope="col" className="right">
                      Share (%)
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {categories.map((cat) => (
                    <tr key={cat.category}>
                      <td>{categoryLabel(cat.category)}</td>
                      <td className="right">{cat.transaction_count}</td>
                      <td className="right">
                        <Amount minor={cat.amount_minor} currency={spending.currency} />
                      </td>
                      <td className="right">{formatShare(cat.percentage)}</td>
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

// -----------------------------------------------------------------------------
// Month-over-Month Spending Trends Section
// -----------------------------------------------------------------------------

function deltaBadge(
  hasComparisonData: boolean,
  momChange: number | null | undefined,
  hasPrevious: boolean,
): { label: string; tone: "success" | "danger" | "neutral"; symbol: string } {
  if (hasComparisonData && momChange !== null && momChange !== undefined) {
    if (momChange > 0) {
      return {
        label: `Increased by ${Math.abs(momChange).toFixed(1)}%`,
        tone: "danger",
        symbol: "▲",
      };
    }
    if (momChange < 0) {
      return {
        label: `Decreased by ${Math.abs(momChange).toFixed(1)}%`,
        tone: "success",
        symbol: "▼",
      };
    }
    return { label: "Unchanged (0.0%)", tone: "neutral", symbol: "●" };
  }
  return {
    label: hasPrevious ? "No spending recorded" : "No previous month data",
    tone: "neutral",
    symbol: "●",
  };
}

function SpendingTrendsSection({ trends }: { trends: SpendingTrendsResponse }) {
  const current = trends.current_month;
  const previous = trends.previous_month;

  const hasComparisonData =
    Boolean(previous) && (current.total_spend_minor > 0 || (previous?.total_spend_minor ?? 0) > 0);

  const barData = [
    ...(previous
      ? [{ period: previous.period, spend: previous.total_spend_minor, fill: "var(--chart-2)" }]
      : []),
    { period: current.period, spend: current.total_spend_minor, fill: "var(--chart-1)" },
  ];

  const delta = deltaBadge(
    hasComparisonData,
    trends.mom_spend_change_percentage,
    Boolean(previous),
  );
  const burnRateChange = trends.burn_rate_change_percentage;
  // Without a previous month every category would show as an "increase" from nothing.
  const topIncreasing = previous ? (trends.top_categories_increasing ?? []) : [];
  const topDecreasing = previous ? (trends.top_categories_decreasing ?? []) : [];

  return (
    <section aria-labelledby="spending-trends-heading" className="insights-section">
      <div className="insights-section__header">
        <div>
          <h2 id="spending-trends-heading">Month-over-Month Trends</h2>
          <p className="muted insights-section__summary">
            Monthly comparison between {previous ? previous.period : "prior cycle"} and{" "}
            {current.period}.
          </p>
        </div>
        <span className={`badge badge--${delta.tone}`}>
          {delta.symbol} {delta.label}
        </span>
      </div>

      <div className="insights-figures">
        <div className="insights-figure">
          <span className="muted insights-figure__label">Daily Burn Rate</span>
          <div className="insights-figure__value">
            <Amount minor={trends.daily_burn_rate_minor} currency={trends.currency} />
            <span className="muted insights-figure__unit"> / day</span>
          </div>
          {burnRateChange !== null && burnRateChange !== undefined && (
            <span
              className={`insights-figure__note ${burnRateChange > 0 ? "error" : burnRateChange < 0 ? "ok" : "muted"}`}
            >
              {burnRateChange > 0 ? "▲ +" : burnRateChange < 0 ? "▼ " : "● "}
              {burnRateChange.toFixed(1)}% vs previous
            </span>
          )}
        </div>

        <div className="insights-figure">
          <span className="muted insights-figure__label">Current Period ({current.period})</span>
          <div className="insights-figure__value">
            <Amount minor={current.total_spend_minor} currency={trends.currency} />
          </div>
          <span className="muted insights-figure__note">
            {current.transaction_count} debit transactions
          </span>
        </div>

        <div className="insights-figure">
          <span className="muted insights-figure__label">
            Previous Period ({previous ? previous.period : "N/A"})
          </span>
          <div className="insights-figure__value">
            {previous ? (
              <Amount minor={previous.total_spend_minor} currency={trends.currency} />
            ) : (
              <span className="muted">—</span>
            )}
          </div>
          <span className="muted insights-figure__note">
            {previous ? `${previous.transaction_count} debit transactions` : "No historical spend"}
          </span>
        </div>
      </div>

      {hasComparisonData ? (
        <div
          role="region"
          aria-label="Month-over-month spending comparison bar chart"
          className="insights-chart insights-chart--bars"
        >
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 500, height: 240 }}
          >
            <BarChart
              data={barData}
              margin={{ top: 10, right: 20, left: 10, bottom: 20 }}
              title="Month-over-month spending"
              desc={`Spending in ${barData.map((row) => `${row.period}: ${formatMinor(row.spend, trends.currency)}`).join(", ")}.`}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="period" stroke="var(--muted)" fontSize={12} />
              <YAxis
                stroke="var(--muted)"
                fontSize={12}
                tickFormatter={(val: number) => `${Math.round(val / 100)}`}
              />
              <Tooltip
                {...TOOLTIP_STYLE}
                formatter={(val) => [formatMinor(Number(val), trends.currency), "Total Spend"]}
              />
              <Bar dataKey="spend" radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <EmptyState message="No month-over-month comparison data available. Additional transaction history is required to display spending trends." />
      )}

      {(topIncreasing.length > 0 || topDecreasing.length > 0) && (
        <div className="insights-shifts">
          <h3>Notable Category Shifts</h3>
          <div className="insights-shifts__grid">
            {topIncreasing.length > 0 && (
              <CategoryShiftBlock
                title="Top Spending Increases"
                items={topIncreasing}
                currency={trends.currency}
                type="increase"
              />
            )}
            {topDecreasing.length > 0 && (
              <CategoryShiftBlock
                title="Top Spending Decreases"
                items={topDecreasing}
                currency={trends.currency}
                type="decrease"
              />
            )}
          </div>
        </div>
      )}

      <details className="insights-fallback">
        <summary>View accessible monthly comparison table</summary>
        <div className="entries">
          <table aria-label="Month-over-month comparison table">
            <thead>
              <tr>
                <th scope="col">Period</th>
                <th scope="col" className="right">
                  Total Spend
                </th>
                <th scope="col" className="right">
                  Transactions
                </th>
                <th scope="col" className="right">
                  Daily Burn Rate
                </th>
              </tr>
            </thead>
            <tbody>
              {!hasComparisonData ? (
                <tr>
                  <td colSpan={4} className="muted insights-table-empty">
                    {!previous
                      ? "No prior month data available for comparison."
                      : "No spending recorded across comparison periods."}
                  </td>
                </tr>
              ) : (
                <>
                  {previous && (
                    <tr>
                      <td>{previous.period} (Previous)</td>
                      <td className="right">
                        <Amount minor={previous.total_spend_minor} currency={trends.currency} />
                      </td>
                      <td className="right">{previous.transaction_count}</td>
                      <td className="right">
                        <Amount minor={previous.daily_burn_rate_minor} currency={trends.currency} />
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td>{current.period} (Current)</td>
                    <td className="right">
                      <Amount minor={current.total_spend_minor} currency={trends.currency} />
                    </td>
                    <td className="right">{current.transaction_count}</td>
                    <td className="right">
                      <Amount minor={current.daily_burn_rate_minor} currency={trends.currency} />
                    </td>
                  </tr>
                </>
              )}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}

interface CategoryShiftBlockProps {
  title: string;
  items: CategoryShiftItem[];
  currency: string;
  type: "increase" | "decrease";
}

function CategoryShiftBlock({ title, items, currency, type }: CategoryShiftBlockProps) {
  const isIncrease = type === "increase";

  return (
    <div className="insights-shift">
      <h4 className="insights-shift__title">{title}</h4>
      <ul className="insights-shift__list">
        {items.map((item) => {
          const changePct = item.change_percentage;
          return (
            <li key={item.category} className="row">
              <span>{categoryLabel(item.category)}</span>
              <span className="insights-legend__figures">
                <span className={`insights-num ${isIncrease ? "error" : "ok"}`}>
                  {isIncrease ? "▲ +" : "▼ "}
                  <Amount minor={Math.abs(item.change_minor)} currency={currency} />
                </span>
                {changePct !== null && changePct !== undefined && (
                  <span className="badge badge--neutral insights-num">
                    {changePct > 0 ? "+" : ""}
                    {changePct.toFixed(1)}%
                  </span>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default Insights;
