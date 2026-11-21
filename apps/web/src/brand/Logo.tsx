/** Product name, tagline and assistant name, in one place for every screen that shows them. */
export const BRAND = "Finora";
export const TAGLINE = "Smart personal banking";
export const ASSISTANT_NAME = `Ask ${BRAND}`;

/**
 * The Finora mark: a navy rounded square holding a lower-case "f" whose crossbar rises in teal (a
 * ledger line that turns upward). Original artwork for this project (ADR-0063); the same drawing is
 * `public/favicon.svg`. Decorative: the wordmark beside it (or the surrounding control) names it.
 */
export function LogoMark({ size = 32 }: { size?: number }) {
  return (
    <svg
      className="logo__mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <rect className="logo__tile" x="0.5" y="0.5" width="31" height="31" rx="8" />
      <path className="logo__stem" d="M13.5 25V12.5A4.5 4.5 0 0 1 18 8h2.5" />
      <path className="logo__rise" d="M9.5 17h7l6-4.5" />
    </svg>
  );
}

/** Mark and wordmark. The wordmark is real text, so it scales, translates and reads aloud. */
export function Logo({ size = 32 }: { size?: number }) {
  return (
    <span className="logo">
      <LogoMark size={size} />
      <span className="logo__word">{BRAND}</span>
    </span>
  );
}
