import { useQuery } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ApiError, api } from "../api/client";
import type { BalanceForecastResponse } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatMinor } from "../lib/money";
import { Alert } from "../ui/Alert";
import { Amount } from "../ui/Amount";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { TOOLTIP_STYLE } from "./insightsChart";
import {
  axisAmount,
  forecastRows,
  forecastSummary,
  obligationLabel,
  shortDate,
} from "./forecastChart";
import type { ForecastRow } from "./forecastChart";

/** TASK-050: 30-day balance projection (TASK-042/043) with its prediction interval. */
export function BalanceForecastSection({ accountId }: { accountId: string }) {
  const query = useQuery({
    queryKey: ["analytics", "forecast", accountId],
    queryFn: () => api.balanceForecast(accountId),
  });

  return (
    <section aria-labelledby="forecast-heading" className="insights-section">
      <div className="insights-section__header">
        <div>
          <h2 id="forecast-heading">Balance Forecast</h2>
          <p className="muted insights-section__summary">
            Your balance over the last 30 days and projected for the next 30, including your
            scheduled payments.
          </p>
        </div>
        <span className="badge badge--warning">Predicted, not exact</span>
      </div>
      <ForecastBody query={query} />
    </section>
  );
}

function ForecastBody({ query }: { query: UseQueryResult<BalanceForecastResponse> }) {
  if (query.isPending) return <LoadingState label="Projecting your balance..." />;
  if (query.isError) {
    if (query.error instanceof ApiError && query.error.status === 422) {
      return (
        <EmptyState message="Not enough account history to forecast yet. Check back after a few days of activity." />
      );
    }
    return (
      <ErrorState
        message={errorMessage(query.error, "Could not load the balance forecast.")}
        onRetry={() => {
          void query.refetch();
        }}
      />
    );
  }
  return <ForecastView forecast={query.data} />;
}

function ForecastView({ forecast }: { forecast: BalanceForecastResponse }) {
  const rows = forecastRows(forecast);
  const summary = forecastSummary(forecast);
  const eom = forecast.end_of_month;
  const money = (minor: number) => formatMinor(minor, forecast.currency);

  return (
    <>
      {eom.low_balance && (
        <Alert tone="warning">
          Your balance may run low by {shortDate(eom.date)}: about {money(eom.predicted_minor)}{" "}
          after scheduled payments. Consider moving money in or rescheduling a payment.
        </Alert>
      )}

      <div className="insights-figures">
        <div className="insights-figure">
          <span className="muted insights-figure__label">
            Projected on {shortDate(eom.date)} (month end)
          </span>
          <div className="insights-figure__value">
            <Amount minor={eom.predicted_minor} currency={forecast.currency} />
          </div>
          <span className="muted insights-figure__note">
            80% range {money(eom.lower_80_minor)} to {money(eom.upper_80_minor)}
          </span>
        </div>
        <div className="insights-figure">
          <span className="muted insights-figure__label">Scheduled payments to month end</span>
          <div className="insights-figure__value">
            <Amount minor={eom.obligations_minor} currency={forecast.currency} />
          </div>
          <span className="muted insights-figure__note">
            Standing orders and loan EMIs, already in the projection
          </span>
        </div>
      </div>

      <p className="muted insights-section__summary" id="forecast-summary">
        {summary}
      </p>

      <div
        role="region"
        aria-label="Balance history and 30-day forecast chart"
        aria-describedby="forecast-summary"
        className="insights-chart insights-chart--forecast"
      >
        <ResponsiveContainer
          width="100%"
          height="100%"
          minWidth={0}
          initialDimension={{ width: 600, height: 280 }}
        >
          <ComposedChart
            data={rows}
            margin={{ top: 10, right: 16, left: 4, bottom: 10 }}
            title="Balance history and forecast"
            desc={summary}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis
              dataKey="date"
              stroke="var(--muted)"
              fontSize={12}
              tickFormatter={shortDate}
              minTickGap={24}
            />
            <YAxis stroke="var(--muted)" fontSize={12} tickFormatter={axisAmount} width={48} />
            <Tooltip
              {...TOOLTIP_STYLE}
              content={({ active, label }) =>
                active ? (
                  <ForecastTooltip
                    row={rows.find((row) => row.date === String(label))}
                    currency={forecast.currency}
                  />
                ) : null
              }
            />
            <Area
              dataKey="band95"
              name="95% range (predicted)"
              stroke="none"
              fill="var(--chart-1)"
              fillOpacity={0.12}
              isAnimationActive={false}
              connectNulls={false}
            />
            <Area
              dataKey="band80"
              name="80% range (predicted)"
              stroke="none"
              fill="var(--chart-1)"
              fillOpacity={0.22}
              isAnimationActive={false}
              connectNulls={false}
            />
            <Line
              dataKey="actual"
              name="Actual balance"
              stroke="var(--chart-1)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              dataKey="predicted"
              name="Predicted balance"
              stroke="var(--chart-1)"
              strokeWidth={2}
              strokeDasharray="6 4"
              dot={false}
              isAnimationActive={false}
            />
            <Scatter
              dataKey="paymentMarker"
              name="Scheduled payment"
              fill="var(--chart-3)"
              shape="diamond"
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <ul className="forecast-legend" aria-label="Chart key">
        <li>
          <span aria-hidden="true" className="forecast-key forecast-key--actual" /> Actual balance
        </li>
        <li>
          <span aria-hidden="true" className="forecast-key forecast-key--predicted" /> Predicted
          balance (not exact)
        </li>
        <li>
          <span aria-hidden="true" className="forecast-key forecast-key--band" /> Shaded: 80% and
          95% prediction ranges
        </li>
        <li>
          <span aria-hidden="true" className="forecast-key forecast-key--payment" /> Scheduled
          payment
        </li>
      </ul>

      {forecast.obligations.length === 0 ? (
        <EmptyState message="No scheduled payments in the next 30 days." />
      ) : (
        <ul className="insights-legend" aria-label="Scheduled payments in the projection">
          {forecast.obligations.map((obligation, index) => (
            <li
              key={`${obligation.scheduled_transfer_id}-${obligation.date}-${index}`}
              className="insights-legend__item"
            >
              <span className="insights-legend__name">
                <span className="insights-legend__label">{shortDate(obligation.date)}</span>
                <span className="muted">{obligationLabel(obligation)}</span>
              </span>
              <strong>
                <Amount minor={-obligation.amount_minor} currency={forecast.currency} />
              </strong>
            </li>
          ))}
        </ul>
      )}

      <details className="insights-fallback">
        <summary>View forecast as a table</summary>
        <div className="entries">
          <table aria-label="Balance forecast table">
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col" className="right">
                  Predicted balance
                </th>
                <th scope="col" className="right">
                  80% range
                </th>
                <th scope="col" className="right">
                  Scheduled payments
                </th>
              </tr>
            </thead>
            <tbody>
              {forecast.points.map((point) => (
                <tr key={point.date}>
                  <td>{shortDate(point.date)}</td>
                  <td className="right">
                    <Amount minor={point.predicted_minor} currency={forecast.currency} />
                  </td>
                  <td className="right">
                    {money(point.lower_80_minor)} to {money(point.upper_80_minor)}
                  </td>
                  <td className="right">
                    {point.obligations_minor > 0 ? money(point.obligations_minor) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

export function ForecastTooltip({
  row,
  currency,
}: {
  row: ForecastRow | undefined;
  currency: string;
}) {
  if (!row) return null;
  const money = (minor: number) => formatMinor(minor, currency);
  return (
    <div className="forecast-tooltip" style={TOOLTIP_STYLE.contentStyle}>
      <strong>{shortDate(row.date)}</strong>
      {row.actual !== null && <div>Actual balance: {money(row.actual)}</div>}
      {row.actual === null && row.predicted !== null && (
        <div>Predicted balance: {money(row.predicted)}</div>
      )}
      {row.lower80 !== null && row.upper80 !== null && (
        <div>
          80% range: {money(row.lower80)} to {money(row.upper80)}
        </div>
      )}
      {row.payments.map((payment, index) => (
        <div key={index}>
          {obligationLabel(payment)}: −{money(payment.amount_minor)}
        </div>
      ))}
      {row.actual === null && <div className="muted">Predicted, not exact</div>}
    </div>
  );
}
