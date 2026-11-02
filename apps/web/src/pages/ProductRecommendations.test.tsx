import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProductRecommendationItem, RecommendationsResponse } from "../api/client";
import { NOT_AVAILABLE_NOTE, productTypeLabel } from "../lib/recommendations";
import { account, customerMe, json, renderApp, urlOf } from "../test-utils";
import { ProductRecommendations } from "./ProductRecommendations";

/** Products and wording as `pbip_ml/recommendations/engine.py` (rules-v1) returns them. */
const savingsAccount: ProductRecommendationItem = {
  product_id: "00000000-0000-4000-a000-000000000001",
  product_type: "SAVINGS",
  title: "Savings account",
  description: "A separate savings account to set money aside from everyday spending.",
  score: 0.81,
  reasons: [
    "You keep about INR 18,000.00 of INR 60,000.00 income each month; a separate account keeps it apart from spending.",
  ],
  model_version: "rules-v1",
  cta_label: "Open account",
};
const fixedDeposit: ProductRecommendationItem = {
  product_id: "00000000-0000-4000-a000-000000000002",
  product_type: "SAVINGS",
  title: "Fixed deposit",
  description: "Lock savings away for a fixed term; the rate is shown before you book.",
  score: 0.77,
  reasons: [
    "Your balance of INR 1,20,000.00 covers 2.9 months of your average spending.",
    "Your financial health score is 68/100.",
  ],
  model_version: "rules-v1",
  cta_label: "Book deposit",
};
const creditLine: ProductRecommendationItem = {
  product_id: "00000000-0000-4000-a000-000000000003",
  product_type: "LOAN",
  title: "Personal credit line",
  description: "Borrow as needed up to an agreed limit, subject to a credit check.",
  score: 0.64,
  reasons: [
    "Loan repayments take 12.0% of your income of INR 60,000.00 a month, within the 45% limit.",
  ],
  model_version: "rules-v1",
  cta_label: "Check eligibility",
};
const familyCover: ProductRecommendationItem = {
  product_id: "00000000-0000-4000-a000-000000000006",
  product_type: "INSURANCE",
  title: "Family protection cover",
  description: "Term life and family health cover; the premium is quoted on application.",
  score: 0.55,
  reasons: ["Your financial health score is 68/100."],
  model_version: "rules-v1",
  cta_label: "Get a quote",
};

function response(items: ProductRecommendationItem[]): RecommendationsResponse {
  return {
    customer_id: "cust-1",
    recommendations: items,
    model_version: "rules-v1",
    evaluated_at: "2026-11-02T09:00:00Z",
  };
}

describe("product recommendations on the accounts page", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let recommendations: () => Promise<Response>;

  beforeEach(() => {
    window.history.replaceState(null, "", "/accounts");
    recommendations = () =>
      Promise.resolve(json(200, response([savingsAccount, fixedDeposit, creditLine])));
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/recommendations")) return recommendations();
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function section() {
    return within(await screen.findByRole("region", { name: "Recommended for you" }));
  }

  it("shows a loading state while the recommendations are on their way", async () => {
    recommendations = () => new Promise<Response>(() => undefined);
    renderApp();
    expect(await (await section()).findByRole("status")).toHaveTextContent(
      "Loading recommendations...",
    );
  });

  it("shows an error with a retry that loads the cards", async () => {
    let calls = 0;
    recommendations = () => {
      calls += 1;
      return Promise.resolve(
        calls === 1
          ? json(500, { code: "internal_error", title: "Internal error" })
          : json(200, response([fixedDeposit])),
      );
    };
    const user = userEvent.setup();
    renderApp();
    const region = await section();
    expect(await region.findByRole("alert")).toBeInTheDocument();
    await user.click(region.getByRole("button", { name: "Try again" }));
    expect(await region.findByRole("heading", { name: "Fixed deposit" })).toBeInTheDocument();
    expect(region.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says so when the API recommends nothing", async () => {
    recommendations = () => Promise.resolve(json(200, response([])));
    renderApp();
    expect(
      await (await section()).findByText("No product recommendations for you right now."),
    ).toBeInTheDocument();
  });

  it("shows one card per recommended product, in the API's order, with its description", async () => {
    renderApp();
    const region = await section();
    await region.findByRole("heading", { name: "Savings account" });
    const titles = region.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual(["Savings account", "Fixed deposit", "Personal credit line"]);
    expect(region.getByText(fixedDeposit.description)).toBeInTheDocument();
    expect(region.getByText("Credit")).toBeInTheDocument();
  });

  it("reveals the API's reasons from a keyboard-operable disclosure", async () => {
    const user = userEvent.setup();
    renderApp();
    const region = await section();
    const heading = await region.findByRole("heading", { name: "Fixed deposit" });
    const card = within(heading.closest("li") ?? heading);
    const toggle = card.getByRole("button", { name: "Why this was recommended for you" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(card.getByText(fixedDeposit.reasons[0] ?? "")).not.toBeVisible();

    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const reasons = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(reasons).not.toBeNull();
    expect(
      within(reasons ?? document.body)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(fixedDeposit.reasons);

    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(card.getByText(fixedDeposit.reasons[0] ?? "")).not.toBeVisible();
  });

  it("opens a savings account straight from the savings card", async () => {
    const user = userEvent.setup();
    renderApp();
    const region = await section();
    // The customer had picked a current account; the recommendation asks for savings again.
    await user.selectOptions(screen.getByLabelText("Account type"), "CHECKING");
    const heading = await region.findByRole("heading", { name: "Savings account" });
    await user.click(
      within(heading.closest("li") ?? heading).getByRole("button", {
        name: "Open account: Savings account",
      }),
    );

    expect(window.location.pathname).toBe("/accounts");
    expect(window.location.search).toBe("?open=SAVINGS");
    const type = screen.getByLabelText("Account type");
    expect(type).toHaveValue("SAVINGS");
    expect(type).toHaveFocus();
    // The form's own button keeps a unique name next to the card's (the E2E journey relies on it).
    expect(screen.getByRole("button", { name: "Open account" })).toBeInTheDocument();
  });

  it("takes the credit line card to the loans page", async () => {
    const user = userEvent.setup();
    renderApp();
    const region = await section();
    const heading = await region.findByRole("heading", { name: "Personal credit line" });
    await user.click(
      within(heading.closest("li") ?? heading).getByRole("button", {
        name: "See our loans: Personal credit line",
      }),
    );
    expect(window.location.pathname).toBe("/loans");
  });

  it("offers no action for a product PBIP cannot open yet", async () => {
    recommendations = () => Promise.resolve(json(200, response([familyCover])));
    renderApp();
    const region = await section();
    const card = within(
      (await region.findByRole("heading", { name: "Family protection cover" })).closest("li") ??
        document.body,
    );
    expect(card.getByText(NOT_AVAILABLE_NOTE)).toBeInTheDocument();
    expect(card.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Why this was recommended for you",
    ]);
  });

  it("sends a fixed deposit to the wealth page, where deposits are booked (TASK-064)", async () => {
    const user = userEvent.setup();
    recommendations = () => Promise.resolve(json(200, response([fixedDeposit])));
    renderApp();
    const region = await section();
    const heading = await region.findByRole("heading", { name: "Fixed deposit" });
    const card = within(heading.closest("li") ?? heading);
    expect(card.queryByText(NOT_AVAILABLE_NOTE)).toBeNull();
    await user.click(card.getByRole("button", { name: "See fixed deposits: Fixed deposit" }));
    expect(window.location.pathname).toBe("/wealth");
  });
});

describe("product recommendations seam for the clickstream", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports the followed product before navigating", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.resolve(json(200, response([creditLine])))),
    );
    const onProductAction = vi.fn<(item: ProductRecommendationItem) => void>();
    const router = createMemoryRouter(
      [
        { path: "/", element: <ProductRecommendations onProductAction={onProductAction} /> },
        { path: "/loans", element: <p>Loans page</p> },
      ],
      { initialEntries: ["/"] },
    );
    const user = userEvent.setup();
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "See our loans: Personal credit line" }),
    );
    expect(onProductAction).toHaveBeenCalledExactlyOnceWith(creditLine);
    expect(await screen.findByText("Loans page")).toBeInTheDocument();
  });
});

describe("productTypeLabel", () => {
  it("names the contract's product types and title-cases one it does not know", () => {
    expect(productTypeLabel("SAVINGS")).toBe("Savings");
    expect(productTypeLabel("LOAN")).toBe("Credit");
    expect(productTypeLabel("INVESTMENT")).toBe("Investment");
    expect(productTypeLabel("INSURANCE")).toBe("Insurance");
    expect(productTypeLabel("CREDIT_CARD")).toBe("Credit card");
  });
});
