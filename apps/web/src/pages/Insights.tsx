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
  SpendingBreakdownResponse,
  SpendingTrendsResponse,
} from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatMinor } from "../lib/money";
import { Alert } from "../ui/Alert";
import { Amount } from "../ui/Amount";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

const RATING_TONES: Record<string, "success" | "info" | "warning" | "danger"> = {
  EXCELLENT: "success",
  GOOD: "info",
  FAIR: "warning",
  NEEDS_ATTENTION: "danger",
};

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
        <PageHeader title="Spending & Financial Health" />
        <LoadingState label="Loading your accounts..." />
      </div>
    );
  }

  if (accountsQuery.isError) {
    return (
      <div className="page">
        <PageHeader title="Spending & Financial Health" />
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
        <PageHeader title="Spending & Financial Health" />
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
        title="Spending & Financial Health"
        description="Personal financial analytics, category breakdown, and financial health score."
        actions={
          accounts.length > 1 ? (
            <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
              <label
                htmlFor="account-selector"
                style={{ margin: 0, fontSize: "var(--text-sm)", color: "var(--muted)" }}
              >
                Account:
              </label>
              <select
                id="account-selector"
                value={activeAccountId ?? ""}
                onChange={(e) => {
                  setSelectedAccountId(e.target.value);
                }}
                style={{
                  minHeight: "var(--control-height)",
                  padding: "0 var(--space-3)",
                  background: "var(--surface)",
                  color: "var(--text)",
                  border: "1px solid var(--border-strong)",
                  borderRadius: "var(--radius)",
                }}
              >
                {accounts.map((acc: AccountOut) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.account_type} ({acc.account_number_masked})
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
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-6)" }}>
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

interface HealthSectionProps {
  health: FinancialHealthScoreResponse;
}

function FinancialHealthSection({ health }: HealthSectionProps) {
  const tone = RATING_TONES[health.rating] ?? "info";
  const recommendations = health.recommendations ?? [];

  return (
    <section
      aria-labelledby="health-score-heading"
      style={{
        padding: "var(--space-5)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
      }}
    >
      <div
        className="row"
        style={{ flexWrap: "wrap", gap: "var(--space-3)", marginBottom: "var(--space-4)" }}
      >
        <div>
          <h2 id="health-score-heading" style={{ margin: 0 }}>
            Financial Health Score
          </h2>
          <p className="muted" style={{ margin: "var(--space-1) 0 0" }}>
            Overall financial health score: {health.overall_score}/100 ({health.rating}). Computed
            from {health.months_observed} observed months of banking history.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
          <span
            style={{
              fontSize: "var(--text-2xl)",
              fontWeight: "var(--weight-strong)",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {health.overall_score}
            <span
              style={{ fontSize: "var(--text-base)", color: "var(--muted)", fontWeight: "normal" }}
            >
              {" "}
              / 100
            </span>
          </span>
          <span
            className={`badge badge--${tone}`}
            style={{ fontSize: "var(--text-sm)", padding: "0 var(--space-3)" }}
          >
            {health.rating}
          </span>
        </div>
      </div>

      {health.insufficient_data && (
        <Alert tone="info">
          Historical data is currently accumulating ({health.months_observed} of{" "}
          {health.window_months} months observed). Component metrics will gain confidence as
          additional monthly cycles complete.
        </Alert>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
          gap: "var(--space-4)",
          marginTop: "var(--space-4)",
        }}
      >
        <HealthComponentCard
          name="Savings"
          component={health.components.savings_rate}
          metricLabel="Savings Rate"
          ariaLabel="Savings score"
        />

        <HealthComponentCard
          name="Buffer"
          component={health.components.buffer_months}
          metricLabel="Cash Buffer"
          ariaLabel="Cash buffer score"
        />

        <HealthComponentCard
          name="Debt"
          component={health.components.emi_to_income}
          metricLabel="Loan / EMI Ratio"
          ariaLabel="Debt repayments score"
        />

        <HealthComponentCard
          name="Expense Volatility"
          component={health.components.expense_volatility}
          metricLabel="Volatility CV"
          ariaLabel="Expense volatility score"
        />
      </div>

      {recommendations.length > 0 && (
        <div
          style={{
            marginTop: "var(--space-5)",
            borderTop: "1px solid var(--border)",
            paddingTop: "var(--space-4)",
          }}
        >
          <h3
            style={{
              fontSize: "var(--text-sm)",
              color: "var(--muted)",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              margin: "0 0 var(--space-2)",
            }}
          >
            Actionable Financial Insights
          </h3>
          <ul
            style={{
              margin: 0,
              paddingLeft: "var(--space-4)",
              color: "var(--text)",
              fontSize: "var(--text-sm)",
            }}
          >
            {recommendations.map((rec, i) => (
              <li key={i} style={{ marginBottom: "var(--space-1)" }}>
                {rec}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

interface ComponentCardProps {
  name: string;
  component: FinancialHealthScoreResponse["components"]["savings_rate"];
  metricLabel: string;
  ariaLabel: string;
}

function HealthComponentCard({ name, component, metricLabel, ariaLabel }: ComponentCardProps) {
  return (
    <div
      style={{
        padding: "var(--space-3) var(--space-4)",
        background: "var(--surface-sunken)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-2)",
      }}
    >
      <div className="row">
        <span style={{ fontWeight: "var(--weight-strong)", fontSize: "var(--text-sm)" }}>
          {name}
        </span>
        <span
          style={{
            fontVariantNumeric: "tabular-nums",
            fontSize: "var(--text-sm)",
            fontWeight: "var(--weight-medium)",
          }}
        >
          {component.score} / {component.max_score} pts
        </span>
      </div>

      <div style={{ width: "100%", margin: "var(--space-1) 0" }}>
        <progress
          value={component.score}
          max={component.max_score}
          aria-label={ariaLabel}
          style={{
            width: "100%",
            height: "8px",
            borderRadius: "var(--radius-sm)",
            accentColor: "var(--brand)",
            display: "block",
          }}
        />
      </div>

      <div className="row" style={{ fontSize: "var(--text-xs)", color: "var(--muted)" }}>
        <span>
          {metricLabel}:{" "}
          <strong style={{ color: "var(--text)" }}>{component.formatted_value}</strong>
        </span>
        <span className="badge badge--neutral" style={{ fontSize: "var(--text-xs)" }}>
          {component.assessment.replace(/_/g, " ")}
        </span>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Category Spending Donut Chart Section
// -----------------------------------------------------------------------------

interface CategorySectionProps {
  spending: SpendingBreakdownResponse;
}

function CategorySpendingSection({ spending }: CategorySectionProps) {
  const categories = spending.categories ?? [];
  const hasCategories = categories.length > 0 && spending.total_spend_minor > 0;

  const chartData = categories.map((cat, idx) => ({
    name: cat.category,
    value: cat.amount_minor,
    percentage: cat.percentage,
    transaction_count: cat.transaction_count,
    fill: CHART_COLORS[idx % CHART_COLORS.length],
  }));

  const summaryText = hasCategories
    ? `Total spending of ${formatMinor(spending.total_spend_minor, spending.currency)} across ${categories.length} categories in ${spending.period}.`
    : `No spending recorded for ${spending.period}.`;

  return (
    <section
      aria-labelledby="category-spending-heading"
      style={{
        padding: "var(--space-5)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
      }}
    >
      <div
        className="row"
        style={{ flexWrap: "wrap", gap: "var(--space-3)", marginBottom: "var(--space-4)" }}
      >
        <div>
          <h2 id="category-spending-heading" style={{ margin: 0 }}>
            Spending by Category
          </h2>
          <p className="muted" style={{ margin: "var(--space-1) 0 0" }}>
            {summaryText}
          </p>
        </div>
        <div style={{ textAlign: "right" }}>
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            Period: {spending.period}
          </span>
          <div style={{ fontSize: "var(--text-lg)", fontWeight: "var(--weight-strong)" }}>
            <Amount minor={spending.total_spend_minor} currency={spending.currency} />
          </div>
        </div>
      </div>

      {!hasCategories ? (
        <EmptyState message="No categorized debit transactions recorded for this period." />
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
              gap: "var(--space-5)",
              alignItems: "center",
              margin: "var(--space-4) 0",
            }}
          >
            <div
              role="region"
              aria-label="Category spending distribution donut chart"
              style={{ width: "100%", height: "260px", minWidth: 0 }}
            >
              <ResponsiveContainer
                width="100%"
                height="100%"
                minWidth={0}
                initialDimension={{ width: 320, height: 260 }}
              >
                <PieChart>
                  <Tooltip
                    formatter={(val, name) => [
                      formatMinor(Number(val), spending.currency),
                      String(name),
                    ]}
                  />
                  <Pie
                    data={chartData}
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

            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {categories.map((cat, idx) => {
                const color = CHART_COLORS[idx % CHART_COLORS.length];
                return (
                  <div
                    key={cat.category}
                    className="row"
                    style={{
                      padding: "var(--space-2) var(--space-3)",
                      background: "var(--surface-sunken)",
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius)",
                      fontSize: "var(--text-sm)",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                      <span
                        aria-hidden="true"
                        style={{
                          width: "12px",
                          height: "12px",
                          borderRadius: "50%",
                          backgroundColor: color,
                          flexShrink: 0,
                          border: "1px solid var(--border-strong)",
                        }}
                      />
                      <span style={{ fontWeight: "var(--weight-medium)" }}>{cat.category}</span>
                      <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
                        ({cat.transaction_count} txs)
                      </span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
                      <span
                        style={{
                          fontWeight: "var(--weight-strong)",
                          fontVariantNumeric: "tabular-nums",
                        }}
                      >
                        <Amount minor={cat.amount_minor} currency={spending.currency} />
                      </span>
                      <span
                        className="badge badge--neutral"
                        style={{ fontVariantNumeric: "tabular-nums" }}
                      >
                        {cat.percentage.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <details style={{ marginTop: "var(--space-4)", fontSize: "var(--text-sm)" }}>
            <summary
              style={{
                cursor: "pointer",
                color: "var(--brand)",
                fontWeight: "var(--weight-medium)",
              }}
            >
              View accessible category table
            </summary>
            <div className="entries table-scroll-container" style={{ marginTop: "var(--space-2)" }}>
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
                      <td>{cat.category}</td>
                      <td className="right">{cat.transaction_count}</td>
                      <td className="right">
                        <Amount minor={cat.amount_minor} currency={spending.currency} />
                      </td>
                      <td className="right">{cat.percentage.toFixed(1)}%</td>
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

interface TrendsSectionProps {
  trends: SpendingTrendsResponse;
}

function SpendingTrendsSection({ trends }: TrendsSectionProps) {
  const current = trends.current_month;
  const previous = trends.previous_month;

  const hasComparisonData =
    Boolean(previous) && (current.total_spend_minor > 0 || (previous?.total_spend_minor ?? 0) > 0);

  const barData = [
    ...(previous
      ? [
          {
            period: previous.period,
            label: `Previous (${previous.period})`,
            spend: previous.total_spend_minor,
            fill: "var(--chart-2)",
          },
        ]
      : []),
    {
      period: current.period,
      label: `Current (${current.period})`,
      spend: current.total_spend_minor,
      fill: "var(--chart-1)",
    },
  ];

  const momChange = trends.mom_spend_change_percentage;
  const absChangeMinor = previous ? current.total_spend_minor - previous.total_spend_minor : null;

  let deltaLabel: string;
  let deltaTone: "success" | "danger" | "neutral" = "neutral";
  let deltaSymbol = "●";

  if (
    hasComparisonData &&
    momChange !== null &&
    momChange !== undefined &&
    absChangeMinor !== null
  ) {
    if (momChange > 0) {
      deltaLabel = `Increased by ${Math.abs(momChange).toFixed(1)}%`;
      deltaTone = "danger";
      deltaSymbol = "▲";
    } else if (momChange < 0) {
      deltaLabel = `Decreased by ${Math.abs(momChange).toFixed(1)}%`;
      deltaTone = "success";
      deltaSymbol = "▼";
    } else {
      deltaLabel = "Unchanged (0.0%)";
      deltaTone = "neutral";
      deltaSymbol = "●";
    }
  } else if (!previous) {
    deltaLabel = "No previous month data";
  } else {
    deltaLabel = "No spending recorded";
  }

  const burnRateChange = trends.burn_rate_change_percentage;
  const topIncreasing = trends.top_categories_increasing ?? [];
  const topDecreasing = trends.top_categories_decreasing ?? [];

  return (
    <section
      aria-labelledby="spending-trends-heading"
      style={{
        padding: "var(--space-5)",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
      }}
    >
      <div
        className="row"
        style={{ flexWrap: "wrap", gap: "var(--space-3)", marginBottom: "var(--space-4)" }}
      >
        <div>
          <h2 id="spending-trends-heading" style={{ margin: 0 }}>
            Month-over-Month Trends
          </h2>
          <p className="muted" style={{ margin: "var(--space-1) 0 0" }}>
            Monthly comparison between {previous ? previous.period : "prior cycle"} and{" "}
            {current.period}.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <span
            className={`badge badge--${deltaTone}`}
            style={{ fontSize: "var(--text-sm)", padding: "0 var(--space-3)" }}
          >
            {deltaSymbol} {deltaLabel}
          </span>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "var(--space-3)",
          margin: "var(--space-4) 0",
        }}
      >
        <div
          style={{
            padding: "var(--space-3) var(--space-4)",
            background: "var(--surface-sunken)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
          }}
        >
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            Daily Burn Rate
          </span>
          <div
            style={{
              fontSize: "var(--text-lg)",
              fontWeight: "var(--weight-strong)",
              marginTop: "var(--space-1)",
            }}
          >
            <Amount minor={trends.daily_burn_rate_minor} currency={trends.currency} />
            <span
              style={{ fontSize: "var(--text-xs)", color: "var(--muted)", fontWeight: "normal" }}
            >
              {" "}
              / day
            </span>
          </div>
          {burnRateChange !== null && burnRateChange !== undefined && (
            <span
              className={burnRateChange > 0 ? "error" : burnRateChange < 0 ? "ok" : "muted"}
              style={{ fontSize: "var(--text-xs)" }}
            >
              {burnRateChange > 0 ? "▲ +" : burnRateChange < 0 ? "▼ " : "● "}
              {burnRateChange.toFixed(1)}% vs previous
            </span>
          )}
        </div>

        <div
          style={{
            padding: "var(--space-3) var(--space-4)",
            background: "var(--surface-sunken)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
          }}
        >
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            Current Period ({current.period})
          </span>
          <div
            style={{
              fontSize: "var(--text-lg)",
              fontWeight: "var(--weight-strong)",
              marginTop: "var(--space-1)",
            }}
          >
            <Amount minor={current.total_spend_minor} currency={trends.currency} />
          </div>
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            {current.transaction_count} debit transactions
          </span>
        </div>

        <div
          style={{
            padding: "var(--space-3) var(--space-4)",
            background: "var(--surface-sunken)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
          }}
        >
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            Previous Period ({previous ? previous.period : "N/A"})
          </span>
          <div
            style={{
              fontSize: "var(--text-lg)",
              fontWeight: "var(--weight-strong)",
              marginTop: "var(--space-1)",
            }}
          >
            {previous ? (
              <Amount minor={previous.total_spend_minor} currency={trends.currency} />
            ) : (
              <span className="muted">—</span>
            )}
          </div>
          <span className="muted" style={{ fontSize: "var(--text-xs)" }}>
            {previous ? `${previous.transaction_count} debit transactions` : "No historical spend"}
          </span>
        </div>
      </div>

      {hasComparisonData ? (
        <div
          role="region"
          aria-label="Month-over-month spending comparison bar chart"
          style={{ width: "100%", height: "240px", margin: "var(--space-4) 0", minWidth: 0 }}
        >
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 500, height: 240 }}
          >
            <BarChart data={barData} margin={{ top: 10, right: 20, left: 10, bottom: 20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="period" stroke="var(--muted)" fontSize={12} />
              <YAxis
                stroke="var(--muted)"
                fontSize={12}
                tickFormatter={(val: number) => `${Math.round(val / 100)}`}
              />
              <Tooltip
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
        <div style={{ marginTop: "var(--space-4)" }}>
          <h3 style={{ fontSize: "var(--text-base)", margin: "0 0 var(--space-3)" }}>
            Notable Category Shifts
          </h3>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
              gap: "var(--space-4)",
            }}
          >
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

      <details style={{ marginTop: "var(--space-4)", fontSize: "var(--text-sm)" }}>
        <summary
          style={{ cursor: "pointer", color: "var(--brand)", fontWeight: "var(--weight-medium)" }}
        >
          View accessible monthly comparison table
        </summary>
        <div className="entries table-scroll-container" style={{ marginTop: "var(--space-2)" }}>
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
                  <td
                    colSpan={4}
                    className="muted"
                    style={{ textAlign: "center", padding: "var(--space-4)" }}
                  >
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
    <div
      style={{
        padding: "var(--space-3)",
        background: "var(--surface-sunken)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
      }}
    >
      <h4
        style={{ margin: "0 0 var(--space-2)", fontSize: "var(--text-sm)", color: "var(--muted)" }}
      >
        {title}
      </h4>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        {items.map((item) => {
          const changePct = item.change_percentage;
          return (
            <div
              key={item.category}
              className="row"
              style={{
                fontSize: "var(--text-sm)",
                borderBottom: "1px solid var(--border)",
                paddingBottom: "var(--space-1)",
              }}
            >
              <span>{item.category}</span>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
                <span
                  className={isIncrease ? "error" : "ok"}
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {isIncrease ? "▲ +" : "▼ "}
                  <Amount minor={Math.abs(item.change_minor)} currency={currency} />
                </span>
                {changePct !== null && changePct !== undefined && (
                  <span
                    className="badge badge--neutral"
                    style={{ fontVariantNumeric: "tabular-nums", fontSize: "var(--text-xs)" }}
                  >
                    {changePct > 0 ? "+" : ""}
                    {changePct.toFixed(1)}%
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default Insights;
