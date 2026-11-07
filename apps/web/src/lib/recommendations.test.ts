import { describe, expect, it } from "vitest";
import type { ProductRecommendationItem } from "../api/client";
import { NOT_AVAILABLE_NOTE, NO_CARDS_NOTE, recommendationAction } from "./recommendations";

function item(productId: string, productType: string): ProductRecommendationItem {
  return {
    product_id: productId,
    product_type: productType,
    title: "t",
    description: "d",
    score: 0.5,
    reasons: ["r"],
    model_version: "rules-v1",
    cta_label: "Open account",
  };
}

describe("every recommendation card gets a working action (ADR-0058 section 10)", () => {
  it.each([
    ["00000000-0000-4000-a000-000000000001", "SAVINGS", "/accounts?open=SAVINGS"],
    ["00000000-0000-4000-a000-000000000002", "SAVINGS", "/wealth?book=fixed-deposit"],
    ["00000000-0000-4000-a000-000000000003", "LOAN", "/loans"],
    [
      "00000000-0000-4000-a000-000000000005",
      "INVESTMENT",
      "/wealth/invest?product=20000000-0000-4000-a000-000000000006",
    ],
    ["00000000-0000-4000-a000-000000000006", "INSURANCE", "/wealth/insurance"],
    // catalogue rows by kind, the id passed on for preselection
    [
      "10000000-0000-4000-a000-000000000001",
      "LOAN",
      "/loans?product=10000000-0000-4000-a000-000000000001",
    ],
    [
      "50000000-0000-4000-a000-000000000004",
      "INVESTMENT",
      "/wealth/invest?product=50000000-0000-4000-a000-000000000004",
    ],
    [
      "30000000-0000-4000-a000-000000000001",
      "INSURANCE",
      "/wealth/insurance?product=30000000-0000-4000-a000-000000000001",
    ],
  ])("%s (%s) goes to %s", (id, type, to) => {
    const action = recommendationAction(item(id, type));
    expect(action.kind === "navigate" ? action.to : action.note).toBe(to);
  });

  it("explains why the rewards card has no apply button", () => {
    expect(recommendationAction(item("00000000-0000-4000-a000-000000000004", "LOAN"))).toEqual({
      kind: "unavailable",
      note: NO_CARDS_NOTE,
    });
  });

  it("never guesses a flow for an unknown kind", () => {
    expect(recommendationAction(item("99999999-0000-4000-a000-000000000001", "MORTGAGE"))).toEqual({
      kind: "unavailable",
      note: NOT_AVAILABLE_NOTE,
    });
  });
});
