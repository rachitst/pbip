import type { BalanceForecastResponse, ForecastObligation } from "../api/client";
import { formatMinor } from "../lib/money";

/** A forecast with figures: the API sends ``INSUFFICIENT_HISTORY`` with no month-end projection
 * for an account too new to forecast (#382), which the page shows as an empty state. */
export type ProjectedForecast = BalanceForecastResponse & {
  end_of_month: NonNullable<BalanceForecastResponse["end_of_month"]>;
};

export function isProjected(forecast: BalanceForecastResponse): forecast is ProjectedForecast {
  return forecast.status !== "INSUFFICIENT_HISTORY" && forecast.end_of_month !== null;
}

/** One x position of the forecast chart: an actual past balance or a predicted day. */
export interface ForecastRow {
  date: string;
  /** Actual end-of-day balance (history only). */
  actual: number | null;
  /** Central estimate; also set on the last actual day so the two lines meet. */
  predicted: number | null;
  band80: [number, number] | null;
  band95: [number, number] | null;
  lower80: number | null;
  upper80: number | null;
  /** Predicted balance on a day with scheduled payments, for the highlighted marker. */
  paymentMarker: number | null;
  payments: ForecastObligation[];
}

/** History then forecast on one axis. The last actual day also carries ``predicted`` so the
 * dashed projection starts where the solid history line ends (no gap between the two). */
export function forecastRows(forecast: BalanceForecastResponse): ForecastRow[] {
  const byDate = new Map<string, ForecastObligation[]>();
  for (const obligation of forecast.obligations) {
    byDate.set(obligation.date, [...(byDate.get(obligation.date) ?? []), obligation]);
  }
  const history: ForecastRow[] = forecast.history.map((point, index) => {
    const last = index === forecast.history.length - 1;
    return {
      date: point.date,
      actual: point.balance_minor,
      predicted: last ? point.balance_minor : null,
      band80: null,
      band95: null,
      lower80: null,
      upper80: null,
      paymentMarker: null,
      payments: [],
    };
  });
  const predicted: ForecastRow[] = forecast.points.map((point) => {
    const payments = byDate.get(point.date) ?? [];
    return {
      date: point.date,
      actual: null,
      predicted: point.predicted_minor,
      band80: [point.lower_80_minor, point.upper_80_minor],
      band95: [point.lower_95_minor, point.upper_95_minor],
      lower80: point.lower_80_minor,
      upper80: point.upper_80_minor,
      paymentMarker: payments.length > 0 ? point.predicted_minor : null,
      payments,
    };
  });
  return [...history, ...predicted];
}

export function obligationLabel(obligation: ForecastObligation): string {
  const kind = obligation.kind === "LOAN_EMI" ? "Loan EMI" : "Scheduled transfer";
  return obligation.reference ? `${kind}: ${obligation.reference}` : kind;
}

const DATE_FORMAT = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});

/** "30 Nov" for an ISO date (the API's dates are UTC calendar days). */
export function shortDate(iso: string): string {
  return DATE_FORMAT.format(new Date(`${iso}T00:00:00Z`));
}

/** The text alternative of the chart: what it shows, in one sentence per fact. */
export function forecastSummary(forecast: ProjectedForecast): string {
  const eom = forecast.end_of_month;
  const money = (minor: number) => formatMinor(minor, forecast.currency);
  const last = forecast.points[forecast.points.length - 1];
  const parts = [
    `Predicted, not exact: by ${shortDate(eom.date)} the balance is projected at about ${money(eom.predicted_minor)} (80% range ${money(eom.lower_80_minor)} to ${money(eom.upper_80_minor)}).`,
  ];
  if (eom.obligations_minor > 0) {
    parts.push(
      `This includes ${money(eom.obligations_minor)} of scheduled payments due before the month end.`,
    );
  }
  if (last) {
    parts.push(
      `In ${forecast.horizon_days} days (${shortDate(last.date)}) it is projected at about ${money(last.predicted_minor)}.`,
    );
  }
  return parts.join(" ");
}

/** Compact axis label: whole rupees in thousands or lakhs, never more precise than the ticks. */
export function axisAmount(minor: number): string {
  const rupees = minor / 100;
  const abs = Math.abs(rupees);
  if (abs >= 1_00_000) return `${(rupees / 1_00_000).toFixed(1)}L`;
  if (abs >= 1_000) return `${(rupees / 1_000).toFixed(0)}k`;
  return rupees.toFixed(0);
}
