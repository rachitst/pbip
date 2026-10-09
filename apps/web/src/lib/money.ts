/** Money is ALWAYS integer minor units (paise). Never use floats for amounts. */
export function formatMinor(minor: number, currency = "INR"): string {
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const major = Math.trunc(abs / 100);
  const cents = String(abs % 100).padStart(2, "0");
  const grouped = new Intl.NumberFormat("en-IN").format(major);
  return `${sign}${currency} ${grouped}.${cents}`;
}

/** Parse a user-typed decimal string ("1,250.50") into minor units. Returns null if invalid. */
export function parseToMinor(input: string): number | null {
  const cleaned = input.replace(/[,\s]/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(cleaned)) return null;
  const [whole = "0", frac = ""] = cleaned.split(".");
  const minor = Number(whole) * 100 + Number(frac.padEnd(2, "0"));
  return Number.isSafeInteger(minor) && minor > 0 ? minor : null;
}
