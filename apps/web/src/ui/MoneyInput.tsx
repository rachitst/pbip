import type { ControlProps } from "./Field";

interface MoneyInputProps extends ControlProps {
  value: string;
  onValueChange: (value: string) => void;
  currency: string;
}

/** Keeps what the user typed as text; callers convert with lib/money `parseToMinor`, so an amount
 * is never held as a float. */
export function MoneyInput({ value, onValueChange, currency, ...control }: MoneyInputProps) {
  return (
    <div className="money-input">
      <span className="money-input__currency" aria-hidden="true">
        {currency}
      </span>
      <input
        {...control}
        value={value}
        onChange={(event) => {
          onValueChange(event.target.value);
        }}
        inputMode="decimal"
        autoComplete="off"
        placeholder="0.00"
      />
    </div>
  );
}
