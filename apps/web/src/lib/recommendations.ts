import type { ProductRecommendationItem } from "../api/client";

/** Where a recommended product's action leads. Only products with a working flow in PBIP get a
 * link; the rest say so instead of offering a button that goes nowhere. */
export type RecommendationAction =
  { kind: "navigate"; to: string; label: string } | { kind: "unavailable"; note: string };

/** Product ids from the recommendation catalogue (`pbip_ml/recommendations/engine.py`). The API
 * types `product_type` as a plain string and two LOAN products (a credit line and a card) need
 * different handling, so the action is chosen by id. An id that is not listed here falls back to
 * "unavailable", never to a guessed flow. */
const SAVINGS_ACCOUNT_ID = "00000000-0000-4000-a000-000000000001";
const FIXED_DEPOSIT_ID = "00000000-0000-4000-a000-000000000002";
const CREDIT_LINE_ID = "00000000-0000-4000-a000-000000000003";

/** The open-account form on /accounts reads this parameter to preselect the account type. */
export const OPEN_ACCOUNT_PARAM = "open";

/** The wealth page reads `?book=fixed-deposit` to open its booking dialog on the catalogue's
 * bookable fixed deposits. The recommended "fixed deposit" is a generic product, not a catalogue
 * row, so the link names the kind of product and the customer picks the deposit in the dialog. */
export const BOOK_PARAM = "book";
export const BOOK_FIXED_DEPOSIT = "fixed-deposit";

export const NOT_AVAILABLE_NOTE = "You cannot apply for this in PBIP yet.";

export function recommendationAction(item: ProductRecommendationItem): RecommendationAction {
  switch (item.product_id) {
    case SAVINGS_ACCOUNT_ID:
      return {
        kind: "navigate",
        to: `/accounts?${OPEN_ACCOUNT_PARAM}=SAVINGS`,
        label: item.cta_label,
      };
    case CREDIT_LINE_ID:
      // PBIP has no credit-line application; the loans page is the borrowing flow that exists, so
      // the label names that page rather than promising a credit-line eligibility check.
      return { kind: "navigate", to: "/loans", label: "See our loans" };
    case FIXED_DEPOSIT_ID:
      // TASK-064: fixed deposits are booked on the wealth page, from the real catalogue; the link
      // opens the booking dialog there, where the customer chooses which deposit to book.
      return {
        kind: "navigate",
        to: `/wealth?${BOOK_PARAM}=${BOOK_FIXED_DEPOSIT}`,
        label: "Book a fixed deposit",
      };
    default:
      return { kind: "unavailable", note: NOT_AVAILABLE_NOTE };
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
