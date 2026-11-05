import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  InsuranceApplicationOut,
  InsuranceProductOut,
  InsuranceQuoteOut,
} from "../api/client";
import { json, urlOf } from "../test-utils";
import InsuranceQuote, { QUOTE_NOTE, REFERRED_NOTE } from "./InsuranceQuote";

const LAKH = 1_00_000_00;
const termLife: InsuranceProductOut = {
  id: "30000000-0000-4000-a000-000000000001",
  code: "TERM-LIFE-CLASSIC",
  insurance_type: "TERM_LIFE",
  name: "Term life cover (illustrative)",
  description: "Pays the cover if you die during the term.",
  currency: "INR",
  cover_min_minor: 25 * LAKH,
  cover_max_minor: 500 * LAKH,
  policy_terms_years: [10, 20, 30],
  indicative_premium: {
    annual_premium_minor: 12_500_00,
    reference_cover_minor: 100 * LAKH,
    reference_age: 30,
    reference_term_years: 30,
  },
  eligibility: { min_entry_age: 18, max_entry_age: 60, max_cover_age: 75, age_bands: [] },
  notes: "Indicative, simulated.",
  catalogue_version: "2026-11-v1",
};
const health: InsuranceProductOut = {
  ...termLife,
  id: "30000000-0000-4000-a000-000000000003",
  code: "HEALTH-INDIVIDUAL",
  insurance_type: "HEALTH",
  name: "Individual health cover (illustrative)",
  cover_min_minor: 3 * LAKH,
  policy_terms_years: [1],
};
const referred: InsuranceQuoteOut = {
  product_id: termLife.id,
  product_name: termLife.name,
  cover_minor: 100 * LAKH,
  term_years: 30,
  age_band: "36-50",
  annual_premium_minor: 20_000_00,
  outcome: "SUBMITTED",
  reason: "At some ages in your band this term would run past the plan's maximum cover age.",
  age_factor_bps: 16_000,
  term_factor_bps: 10_000,
  quote_version: "IQ-1",
};
const application: InsuranceApplicationOut = {
  id: "app-1",
  product_id: termLife.id,
  product_name: termLife.name,
  cover_minor: 100 * LAKH,
  term_years: 30,
  age_band: "36-50",
  annual_premium_minor: 20_000_00,
  status: "SUBMITTED",
  decision_reason: referred.reason,
  quote_version: "IQ-1",
  created_at: "2026-11-05T06:00:00Z",
};

function mockApi(applications: InsuranceApplicationOut[] = []) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = urlOf(input);
    if (url === "/api/v1/wealth/insurance") return Promise.resolve(json(200, [termLife, health]));
    if (url === "/api/v1/wealth/insurance/quote") return Promise.resolve(json(200, referred));
    if (url === "/api/v1/wealth/insurance/applications") {
      return Promise.resolve(json(200, applications));
    }
    return Promise.resolve(json(404, { title: "not mocked" }));
  });
}

function renderPage(path = "/wealth/insurance") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <InsuranceQuote />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Insurance quote (ADR-0058 section 8)", () => {
  it("preselects the plan from the link and says the quote is simulated", async () => {
    mockApi();
    renderPage(`/wealth/insurance?product=${health.id}`);
    const form = within(await screen.findByRole("region", { name: "Insurance quote" }));
    expect(form.getByLabelText("Plan")).toHaveValue(health.id);
    expect(form.getByText(QUOTE_NOTE)).toBeInTheDocument();
    expect(await screen.findByText("You have not applied for any insurance yet.")).toBeVisible();
  });

  it("needs cover, a declared band and consent before quoting, then applies with a key", async () => {
    const user = userEvent.setup({ delay: null });
    const fetchMock = mockApi();
    renderPage();
    const form = within(await screen.findByRole("region", { name: "Insurance quote" }));
    const quote = form.getByRole("button", { name: "Get a quote" });
    await user.type(form.getByLabelText("Cover"), "10000000");
    await user.selectOptions(form.getByLabelText(/Your age band/), "36-50");
    expect(quote).toBeDisabled(); // no consent yet
    await user.click(form.getByRole("checkbox", { name: /age band being used/ }));
    expect(form.getByRole("button", { name: "Apply" })).toBeDisabled(); // quote first
    await user.click(quote);
    const result = within(await form.findByRole("region", { name: "Quote" }));
    expect(result.getByText("INR 20,000.00")).toBeInTheDocument();
    expect(result.getByText("Referred")).toBeInTheDocument();
    const quoteCall = fetchMock.mock.calls.find(([i]) => urlOf(i).endsWith("/insurance/quote"));
    const sent = quoteCall?.[1]?.body;
    expect(JSON.parse(typeof sent === "string" ? sent : "null")).toEqual({
      product_id: termLife.id,
      cover_minor: 100 * LAKH,
      term_years: 30,
      age_band: "36-50",
      age_band_consent: true,
    });

    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url === "/api/v1/wealth/insurance/applications" && init?.method === "POST") {
        return Promise.resolve(json(201, application));
      }
      if (url === "/api/v1/wealth/insurance/applications") {
        return Promise.resolve(json(200, [application]));
      }
      return Promise.resolve(json(200, [termLife, health]));
    });
    await user.click(form.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(REFERRED_NOTE)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([input, init]) => urlOf(input).endsWith("/applications") && init?.method === "POST",
    );
    expect(new Headers(post?.[1]?.headers).get("Idempotency-Key")).toBeTruthy();
    const table = await screen.findByRole("table");
    expect(within(table).getByText(termLife.name)).toBeInTheDocument();
  });

  it("lists applications with their status", async () => {
    mockApi([{ ...application, status: "DECLINED", annual_premium_minor: null }]);
    renderPage();
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Declined")).toBeInTheDocument();
  });
});
