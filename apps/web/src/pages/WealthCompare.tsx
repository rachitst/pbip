import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import type { InsuranceProductOut, RiskLevel, WealthProductOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatMinor } from "../lib/money";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { formatRate } from "./Loans";
import { formatShareBps, tenureLabel } from "./Wealth";
import "./WealthCompare.css";

export const MIN_COMPARE = 2;
export const MAX_COMPARE = 4;
export const NOT_APPLICABLE = "Not applicable";

/** One row of the comparison: a wealth product or an insurance product, never a merged shape. */
export type Comparable =
  | { kind: "WEALTH"; key: string; product: WealthProductOut }
  | { kind: "INSURANCE"; key: string; product: InsuranceProductOut };

const WEALTH_TYPE: Record<WealthProductOut["product_type"], string> = {
  FIXED_DEPOSIT: "Fixed deposit",
  MUTUAL_FUND: "Mutual fund",
  SOVEREIGN_GOLD_BOND: "Sovereign gold bond",
};
const INSURANCE_TYPE: Record<InsuranceProductOut["insurance_type"], string> = {
  TERM_LIFE: "Term life insurance",
  HEALTH: "Health insurance",
};
const RISK_LABEL: Record<RiskLevel, string> = {
  LOW: "Low",
  MODERATE: "Moderate",
  HIGH: "High",
};

/** Every field returns text the catalogue states, or NOT_APPLICABLE / "Not stated": a value
 * is never invented for a product type it does not apply to. */
export const FIELDS: { label: string; value: (item: Comparable) => string }[] = [
  {
    label: "Type",
    value: (item) =>
      item.kind === "WEALTH"
        ? WEALTH_TYPE[item.product.product_type]
        : INSURANCE_TYPE[item.product.insurance_type],
  },
  {
    label: "Yield",
    value: (item) => {
      if (item.kind === "INSURANCE") return NOT_APPLICABLE;
      const p = item.product;
      if (p.annual_rate_bps === null) return "Market-linked: no stated yield";
      if (p.yield_basis === "FIXED_COUPON") return `${formatRate(p.annual_rate_bps)} coupon`;
      return p.apy_bps === null
        ? formatRate(p.annual_rate_bps)
        : `${formatRate(p.annual_rate_bps)} (APY ${formatShareBps(p.apy_bps)})`;
    },
  },
  {
    label: "Tenure or policy term",
    value: (item) => {
      if (item.kind === "INSURANCE") {
        const terms = item.product.policy_terms_years;
        if (terms.length === 1 && terms[0] === 1) return "1 year, renewable";
        return `${terms.map(String).join(", ")} years`;
      }
      const months = item.product.tenures_months;
      return months.length === 0 ? "Open-ended" : months.map(tenureLabel).join(", ");
    },
  },
  {
    label: "Lock-in",
    value: (item) => {
      if (item.kind === "INSURANCE") return NOT_APPLICABLE;
      const months = item.product.min_lock_in_months;
      return months === 0 ? "None" : tenureLabel(months);
    },
  },
  {
    label: "Risk level",
    value: (item) =>
      item.kind === "WEALTH" ? RISK_LABEL[item.product.risk_level] : NOT_APPLICABLE,
  },
  {
    label: "Minimum amount",
    value: (item) => {
      if (item.kind === "INSURANCE") return NOT_APPLICABLE;
      const min = item.product.min_amount_minor;
      return min === null ? "Not stated" : formatMinor(min, "INR");
    },
  },
  {
    label: "Cover",
    value: (item) =>
      item.kind === "WEALTH"
        ? NOT_APPLICABLE
        : `${formatMinor(item.product.cover_min_minor, item.product.currency)} to ${formatMinor(
            item.product.cover_max_minor,
            item.product.currency,
          )}`,
  },
  {
    label: "Indicative premium",
    value: (item) => {
      if (item.kind === "WEALTH") return NOT_APPLICABLE;
      const p = item.product.indicative_premium;
      const currency = item.product.currency;
      return (
        `${formatMinor(p.annual_premium_minor, currency)} a year for ` +
        `${formatMinor(p.reference_cover_minor, currency)} cover, age ${String(p.reference_age)}, ` +
        `${String(p.reference_term_years)}-year term`
      );
    },
  },
  {
    label: "Entry age",
    value: (item) =>
      item.kind === "WEALTH"
        ? NOT_APPLICABLE
        : `${String(item.product.eligibility.min_entry_age)} to ${String(
            item.product.eligibility.max_entry_age,
          )}`,
  },
];

/** What the selection status line says; null when a comparison can be shown with nothing to add. */
export function selectionMessage(count: number): string {
  if (count < MIN_COMPARE) {
    const more = MIN_COMPARE - count;
    return `Select at least ${String(MIN_COMPARE)} products to compare (${String(more)} more).`;
  }
  if (count >= MAX_COMPARE) {
    return `You are comparing the maximum of ${String(MAX_COMPARE)} products. Remove one to add another.`;
  }
  return `Comparing ${String(count)} products. You can add up to ${String(MAX_COMPARE - count)} more.`;
}

function ProductPicker({
  legend,
  items,
  selected,
  onToggle,
}: {
  legend: string;
  items: Comparable[];
  selected: string[];
  onToggle: (key: string) => void;
}) {
  const full = selected.length >= MAX_COMPARE;
  return (
    <fieldset className="compare-picker">
      <legend>{legend}</legend>
      {items.length === 0 ? (
        <p className="muted">None in the catalogue.</p>
      ) : (
        items.map((item) => {
          const checked = selected.includes(item.key);
          return (
            <label key={item.key} className="compare-picker__option">
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && full}
                onChange={() => {
                  onToggle(item.key);
                }}
              />
              <span>{item.product.name}</span>
            </label>
          );
        })
      )}
    </fieldset>
  );
}

function ComparisonTable({ items }: { items: Comparable[] }) {
  const captionId = useId();
  return (
    <div className="compare-scroll" role="region" aria-labelledby={captionId} tabIndex={0}>
      <table className="compare-table">
        <caption id={captionId}>Product comparison</caption>
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Feature</span>
            </th>
            {items.map((item) => (
              <th key={item.key} scope="col">
                {item.product.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {FIELDS.map((field) => (
            <tr key={field.label}>
              <th scope="row">{field.label}</th>
              {items.map((item) => {
                const text = field.value(item);
                return (
                  <td key={item.key} className={text === NOT_APPLICABLE ? "muted" : undefined}>
                    {text}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** TASK-098: compare 2 to 4 wealth and insurance products side by side. Both lists come from the
 * seeded catalogues (GET /wealth/products, GET /wealth/insurance, ADR-0025 and ADR-0028). */
export function WealthCompare() {
  const wealth = useQuery({
    queryKey: ["wealth-products", ""],
    queryFn: () => api.wealthProducts(),
  });
  const insurance = useQuery({ queryKey: ["insurance-products"], queryFn: api.insuranceProducts });
  const [selected, setSelected] = useState<string[]>([]);

  function toggle(key: string) {
    setSelected((current) => {
      if (current.includes(key)) return current.filter((k) => k !== key);
      return current.length >= MAX_COMPARE ? current : [...current, key];
    });
  }

  let body: ReactNode;
  if (wealth.isPending || insurance.isPending) {
    body = <LoadingState label="Loading products to compare..." />;
  } else if (wealth.isError || insurance.isError) {
    body = (
      <ErrorState
        message={errorMessage(wealth.error ?? insurance.error, "Could not load the products.")}
        onRetry={() => {
          void wealth.refetch();
          void insurance.refetch();
        }}
      />
    );
  } else {
    const investments: Comparable[] = wealth.data.map((p) => ({
      kind: "WEALTH",
      key: `wealth:${p.id}`,
      product: p,
    }));
    const policies: Comparable[] = insurance.data.map((p) => ({
      kind: "INSURANCE",
      key: `insurance:${p.id}`,
      product: p,
    }));
    const all = [...investments, ...policies];
    const chosen = selected.flatMap((key) => all.filter((item) => item.key === key));
    body =
      all.length === 0 ? (
        <EmptyState message="There are no products to compare yet." />
      ) : (
        <>
          <div className="compare-pickers">
            <ProductPicker
              legend="Investments"
              items={investments}
              selected={selected}
              onToggle={toggle}
            />
            <ProductPicker
              legend="Insurance"
              items={policies}
              selected={selected}
              onToggle={toggle}
            />
          </div>
          <p role="status" className="compare-status">
            {selectionMessage(chosen.length)}
          </p>
          {chosen.length >= MIN_COMPARE && <ComparisonTable items={chosen} />}
        </>
      );
  }

  return (
    <div className="page">
      <PageHeader
        title="Compare products"
        description="Pick two to four investments or insurance plans to see them side by side."
        actions={<Link to="/wealth">Back to wealth</Link>}
      />
      <p className="muted">
        Rates and premiums are indicative and simulated for this demo bank. Premiums are for the
        reference profile shown, not a quote: PBIP does no underwriting. Market-linked funds show no
        yield because PBIP has no market data.
      </p>
      {body}
    </div>
  );
}

export default WealthCompare;
