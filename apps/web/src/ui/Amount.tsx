import { formatMinor } from "../lib/money";

interface AmountProps {
  minor: number;
  currency: string;
  /** Prefix credits with "+" so direction is never shown by colour alone. */
  credit?: boolean;
}

export function Amount({ minor, currency, credit = false }: AmountProps) {
  const text = formatMinor(minor, currency);
  return (
    <span className={credit ? "amount amount--credit" : "amount"}>
      {credit && minor > 0 ? `+${text}` : text}
    </span>
  );
}
