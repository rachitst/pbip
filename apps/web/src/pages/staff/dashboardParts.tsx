import type { ReactNode } from "react";
import { Field } from "../../ui/Field";

export interface Option<T extends string> {
  value: T;
  label: string;
}

/** A labelled select for the filter row above a dashboard (TASK-100, TASK-101). */
export function FilterSelect<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly Option<T>[];
}) {
  return (
    <Field label={label}>
      {(control) => (
        <select
          {...control}
          value={value}
          onChange={(e) => {
            const chosen = options.find((o) => o.value === e.target.value);
            if (chosen) onChange(chosen.value);
          }}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

/** One headline number with its definition always visible (never a hover-only tooltip). */
export function Metric({
  label,
  value,
  definition,
  tone,
}: {
  label: string;
  value: ReactNode;
  definition: string;
  tone?: "warning" | "danger" | undefined;
}) {
  return (
    <div className={tone ? `metric metric--${tone}` : "metric"}>
      <dt>{label}</dt>
      <dd className="metric__value">{value}</dd>
      <dd className="metric__definition">{definition}</dd>
    </div>
  );
}

export const PERIOD_OPTIONS = [
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
] as const;
