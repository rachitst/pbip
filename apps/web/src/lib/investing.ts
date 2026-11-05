/** Units arithmetic for simulated investing (ADR-0058 section 3). The API counts units in integer
 * micro-units (1 unit = 1,000,000) and money in integer paise; these helpers never use floats. */
export const MICRO_PER_UNIT = 1_000_000;
const MICRO_DIGITS = 6;

/** "12.345678" -> "12.345678", "3.000000" -> "3", always with integer arithmetic. */
export function formatUnits(unitsMicro: number): string {
  const whole = Math.trunc(unitsMicro / MICRO_PER_UNIT);
  const fraction = String(unitsMicro % MICRO_PER_UNIT)
    .padStart(MICRO_DIGITS, "0")
    .replace(/0+$/, "");
  return fraction === "" ? String(whole) : `${String(whole)}.${fraction}`;
}

/** Parse what the customer typed as a number of units into micro-units, or null if it is not a
 * positive number with at most six decimals (no decimals at all when `wholeOnly`). */
export function parseUnits(input: string, wholeOnly: boolean): number | null {
  const text = input.trim();
  const pattern = wholeOnly ? /^\d{1,9}$/ : /^\d{1,9}(\.\d{1,6})?$/;
  if (!pattern.test(text)) return null;
  const [whole = "0", fraction = ""] = text.split(".");
  const micro =
    Number.parseInt(whole, 10) * MICRO_PER_UNIT +
    Number.parseInt(fraction.padEnd(MICRO_DIGITS, "0"), 10);
  return micro > 0 ? micro : null;
}

/** Units an amount buys at a price, rounded down exactly as the server does (BigInt: exact). */
export function unitsForAmount(amountMinor: number, priceMinor: number): number {
  return Number((BigInt(amountMinor) * BigInt(MICRO_PER_UNIT)) / BigInt(priceMinor));
}

/** What units are worth at a price, rounded down exactly as the server does. */
export function valueOfUnits(unitsMicro: number, priceMinor: number): number {
  return Number((BigInt(unitsMicro) * BigInt(priceMinor)) / BigInt(MICRO_PER_UNIT));
}

/** Basis points of gain or loss to "+1.23%" / "-0.50%" (integer arithmetic). */
export function formatGainBps(bps: number): string {
  const sign = bps > 0 ? "+" : bps < 0 ? "-" : "";
  const abs = Math.abs(bps);
  return `${sign}${String(Math.trunc(abs / 100))}.${String(abs % 100).padStart(2, "0")}%`;
}

export const SIMULATED_PRICE_NOTE =
  "Prices are simulated by PBIP's price model, one price per day. They are not market data.";
