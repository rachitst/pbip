import type { ProductRecommendationItem } from "../api/client";

/** Where a recommended product's action leads. Only products with a working flow in PBIP get a
 * link; the rest say so instead of offering a button that goes nowhere. */
export type RecommendationAction =
  { kind: "navigate"; to: string; label: string } | { kind: "unavailable"; note: string };

/** Product ids from the recommendation engine's own catalogue (`pbip_ml/recommendations/engine.py`).
 * Anything else is chosen by `product_type`, with the id passed on so the destination can preselect
 * it when it is one of its catalogue rows (a catalogue loan, a fund, stock or bond, an insurance
 * plan); a destination that does not know the id simply opens unselected (ADR-0058 section 10). */
const SAVINGS_ACCOUNT_ID = "00000000-0000-4000-a000-000000000001";
const FIXED_DEPOSIT_ID = "00000000-0000-4000-a000-000000000002";
const CREDIT_LINE_ID = "00000000-0000-4000-a000-000000000003";
const REWARDS_CARD_ID = "00000000-0000-4000-a000-000000000004";
const INDEX_SIP_ID = "00000000-0000-4000-a000-000000000005";
const FAMILY_COVER_ID = "00000000-0000-4000-a000-000000000006";
/** The Nifty 50 index fund's catalogue row (migration 0018): what the index SIP card invests in. */
export const INDEX_FUND_PRODUCT_ID = "20000000-0000-4000-a000-000000000006";

/** The open-account form on /accounts reads this parameter to preselect the account type. */
export const OPEN_ACCOUNT_PARAM = "open";

/** The wealth page reads `?book=fixed-deposit` to open its booking dialog on the catalogue's
 * bookable fixed deposits. The recommended "fixed deposit" is a generic product, not a catalogue
 * row, so the link names the kind of product and the customer picks the deposit in the dialog. */
export const BOOK_PARAM = "book";
export const BOOK_FIXED_DEPOSIT = "fixed-deposit";

/** `/loans`, `/wealth/invest` and `/wealth/insurance` preselect `?product=<id>` when it is theirs. */
export const PRODUCT_PARAM = "product";

export const NOT_AVAILABLE_NOTE = "You cannot apply for this in PBIP yet.";
/** ADR-0058 section 10: PBIP issues no cards (no card network, no card ledger), so the rewards card
 * gets a plain note rather than an "apply" button that would record nothing. */
export const NO_CARDS_NOTE = "PBIP does not issue cards yet, so there is nothing to apply for.";

function withProduct(path: string, productId: string): string {
  const params = new URLSearchParams({ [PRODUCT_PARAM]: productId });
  return `${path}?${params.toString()}`;
}

function byKind(item: ProductRecommendationItem): RecommendationAction {
  switch (item.product_type) {
    case "LOAN":
      return {
        kind: "navigate",
        to: withProduct("/loans", item.product_id),
        label: "Apply for this loan",
      };
    case "INVESTMENT":
      return {
        kind: "navigate",
        to: withProduct("/wealth/invest", item.product_id),
        label: "Invest",
      };
    case "INSURANCE":
      return {
        kind: "navigate",
        to: withProduct("/wealth/insurance", item.product_id),
        label: "Get a quote",
      };
    default:
      return { kind: "unavailable", note: NOT_AVAILABLE_NOTE };
  }
}

export function recommendationAction(item: ProductRecommendationItem): RecommendationAction {
  switch (item.product_id) {
    case SAVINGS_ACCOUNT_ID:
      return {
        kind: "navigate",
        to: `/accounts?${OPEN_ACCOUNT_PARAM}=SAVINGS`,
        label: item.cta_label,
      };
    case CREDIT_LINE_ID:
      // PBIP has no credit-line product of its own; the loans page is the borrowing flow that
      // exists, so the label names that page rather than promising a credit-line check.
      return { kind: "navigate", to: "/loans", label: "See our loans" };
    case FIXED_DEPOSIT_ID:
      // TASK-064: fixed deposits are booked on the wealth page, from the real catalogue; the link
      // opens the booking dialog there, where the customer chooses which deposit to book.
      return {
        kind: "navigate",
        to: `/wealth?${BOOK_PARAM}=${BOOK_FIXED_DEPOSIT}`,
        label: "Book a fixed deposit",
      };
    case INDEX_SIP_ID:
      return {
        kind: "navigate",
        to: withProduct("/wealth/invest", INDEX_FUND_PRODUCT_ID),
        label: "Invest in the index fund",
      };
    case FAMILY_COVER_ID:
      return { kind: "navigate", to: "/wealth/insurance", label: "Get a quote" };
    case REWARDS_CARD_ID:
      return { kind: "unavailable", note: NO_CARDS_NOTE };
    default:
      return byKind(item);
  }
}

const PRODUCT_TYPES: Record<string, string> = {
  SAVINGS: "Savings",
  LOAN: "Credit",
  INVESTMENT: "Investment",
  INSURANCE: "Insurance",
};

/** `product_type` is a free string in the contract; an unknown value is shown title-cased. */
export function productTypeLabel(productType: string): string {
  const known = PRODUCT_TYPES[productType];
  if (known !== undefined) return known;
  const lower = productType.toLowerCase().replace(/_/g, " ");
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

/** ADR-0049: shown under the cards when the API says a model trained on SIMULATED uptake ordered
 * them (``score_from_simulated_data``). The wording is fixed here, never built from the API. */
export const SIMULATED_ORDER_NOTE =
  "The order of these suggestions comes from a model trained on simulated product take-up, not on real customers' choices. Every product shown meets our eligibility rules.";
