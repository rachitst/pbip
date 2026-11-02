import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InsuranceProductOut, WealthProductOut } from "../api/client";
import { json, urlOf } from "../test-utils";
import { FIELDS, NOT_APPLICABLE, WealthCompare, selectionMessage } from "./WealthCompare";
import type { Comparable } from "./WealthCompare";

const regular: WealthProductOut = {
  id: "fd-regular",
  code: "FD-REGULAR",
  product_type: "FIXED_DEPOSIT",
  name: "Fixed deposit, 1 to 3 years",
  description: "Cumulative deposit.",
  risk_level: "LOW",
  yield_basis: "GUARANTEED",
  annual_rate_bps: 710,
  apy_bps: 729,
  tenures_months: [12, 18, 24, 36],
  min_lock_in_months: 0,
  min_amount_minor: 1_000_000,
  max_amount_minor: 1_000_000_000,
  premature_closure_allowed: true,
  premature_penalty_bps: 100,
  bookable: true,
  notes: "Indicative, simulated rate.",
  catalogue_version: "2026-11-v1",
};
const elss: WealthProductOut = {
  ...regular,
  id: "mf-elss",
  code: "MF-ELSS",
  product_type: "MUTUAL_FUND",
  name: "ELSS tax-saver fund (illustrative)",
  risk_level: "HIGH",
  yield_basis: "MARKET_LINKED",
  annual_rate_bps: null,
  apy_bps: null,
  tenures_months: [],
  min_lock_in_months: 36,
  min_amount_minor: 50_000,
  max_amount_minor: null,
  premature_closure_allowed: false,
  premature_penalty_bps: null,
  bookable: false,
};
const gold: WealthProductOut = {
  ...regular,
  id: "sgb",
  code: "SGB",
  product_type: "SOVEREIGN_GOLD_BOND",
  name: "Sovereign gold bond (illustrative)",
  risk_level: "MODERATE",
  yield_basis: "FIXED_COUPON",
  annual_rate_bps: 250,
  apy_bps: 250,
  tenures_months: [96],
  min_lock_in_months: 60,
  min_amount_minor: null,
  max_amount_minor: null,
  bookable: false,
};
const term: InsuranceProductOut = {
  id: "term-classic",
  code: "TERM-LIFE-CLASSIC",
  insurance_type: "TERM_LIFE",
  name: "Term life cover (illustrative)",
  description: "Pure protection.",
  currency: "INR",
  cover_min_minor: 250_000_000,
  cover_max_minor: 5_000_000_000,
  policy_terms_years: [10, 20, 30],
  indicative_premium: {
    annual_premium_minor: 1_250_000,
    reference_cover_minor: 1_000_000_000,
    reference_age: 30,
    reference_term_years: 30,
  },
  eligibility: {
    min_entry_age: 18,
    max_entry_age: 60,
    max_cover_age: 75,
    age_bands: [{ age_band: "18-25", whole_band_eligible: true }],
  },
  notes: "Indicative, simulated premium. PBIP does no underwriting.",
  catalogue_version: "2026-11-v1",
};
const health: InsuranceProductOut = {
  ...term,
  id: "health-individual",
  code: "HEALTH-INDIVIDUAL",
  insurance_type: "HEALTH",
  name: "Individual health cover (illustrative)",
  cover_min_minor: 30_000_000,
  cover_max_minor: 1_000_000_000,
  policy_terms_years: [1],
  indicative_premium: {
    annual_premium_minor: 780_000,
    reference_cover_minor: 50_000_000,
    reference_age: 30,
    reference_term_years: 1,
  },
  eligibility: { ...term.eligibility, max_entry_age: 65, max_cover_age: null },
};

interface Options {
  products?: () => Response;
  insurance?: () => Response;
}

function mockApi(options: Options = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = urlOf(input);
    if (url === "/api/v1/wealth/products") {
      return Promise.resolve(options.products?.() ?? json(200, [regular, elss, gold]));
    }
    if (url === "/api/v1/wealth/insurance") {
      return Promise.resolve(options.insurance?.() ?? json(200, [term, health]));
    }
    return Promise.resolve(json(404, { title: "not mocked" }));
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WealthCompare />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function valueOf(label: string, item: Comparable): string {
  const field = FIELDS.find((f) => f.label === label);
  if (!field) throw new Error(`no field ${label}`);
  return field.value(item);
}

/** The table's cells for one row, read through the row header (accessible table semantics). */
function rowCells(table: HTMLElement, header: string): string[] {
  const row = within(table).getByRole("rowheader", { name: header }).closest("tr");
  if (!row) throw new Error(`no row ${header}`);
  return within(row)
    .getAllByRole("cell")
    .map((cell) => cell.textContent);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WealthCompare (TASK-098)", () => {
  const fd: Comparable = { kind: "WEALTH", key: "w1", product: regular };
  const fund: Comparable = { kind: "WEALTH", key: "w2", product: elss };
  const bond: Comparable = { kind: "WEALTH", key: "w3", product: gold };
  const life: Comparable = { kind: "INSURANCE", key: "i1", product: term };
  const cover: Comparable = { kind: "INSURANCE", key: "i2", product: health };

  it("formats every field from the catalogue and says Not applicable instead of inventing", () => {
    expect(valueOf("Yield", fd)).toBe("7.10% p.a. (APY 7.29%)");
    expect(valueOf("Yield", fund)).toBe("Market-linked: no stated yield");
    expect(valueOf("Yield", bond)).toBe("2.50% p.a. coupon");
    expect(valueOf("Yield", life)).toBe(NOT_APPLICABLE);
    expect(valueOf("Tenure or policy term", fd)).toBe("1 year, 18 months, 2 years, 3 years");
    expect(valueOf("Tenure or policy term", fund)).toBe("Open-ended");
    expect(valueOf("Tenure or policy term", life)).toBe("10, 20, 30 years");
    expect(valueOf("Tenure or policy term", cover)).toBe("1 year, renewable");
    expect(valueOf("Lock-in", fd)).toBe("None");
    expect(valueOf("Lock-in", fund)).toBe("3 years");
    expect(valueOf("Lock-in", cover)).toBe(NOT_APPLICABLE);
    expect(valueOf("Risk level", fund)).toBe("High");
    expect(valueOf("Risk level", life)).toBe(NOT_APPLICABLE);
    expect(valueOf("Minimum amount", fd)).toBe("INR 10,000.00");
    expect(valueOf("Minimum amount", bond)).toBe("Not stated");
    expect(valueOf("Minimum amount", life)).toBe(NOT_APPLICABLE);
    expect(valueOf("Cover", life)).toBe("INR 25,00,000.00 to INR 5,00,00,000.00");
    expect(valueOf("Cover", fd)).toBe(NOT_APPLICABLE);
    expect(valueOf("Indicative premium", life)).toBe(
      "INR 12,500.00 a year for INR 1,00,00,000.00 cover, age 30, 30-year term",
    );
    expect(valueOf("Indicative premium", cover)).toBe(
      "INR 7,800.00 a year for INR 5,00,000.00 cover, age 30, 1-year term",
    );
    expect(valueOf("Indicative premium", fund)).toBe(NOT_APPLICABLE);
    expect(valueOf("Entry age", cover)).toBe("18 to 65");
    expect(valueOf("Entry age", fd)).toBe(NOT_APPLICABLE);
  });

  it("explains the 2 to 4 selection limit", () => {
    expect(selectionMessage(0)).toBe("Select at least 2 products to compare (2 more).");
    expect(selectionMessage(1)).toBe("Select at least 2 products to compare (1 more).");
    expect(selectionMessage(3)).toBe("Comparing 3 products. You can add up to 1 more.");
    expect(selectionMessage(4)).toMatch(/maximum of 4 products\. Remove one/);
  });

  it("shows a loading state, then both catalogues to pick from", async () => {
    const fetchMock = mockApi();
    renderPage();
    expect(screen.getByText("Loading products to compare...")).toBeInTheDocument();
    const investments = await screen.findByRole("group", { name: "Investments" });
    const insurance = screen.getByRole("group", { name: "Insurance" });
    expect(within(investments).getAllByRole("checkbox")).toHaveLength(3);
    expect(within(insurance).getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("Select at least 2 products");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([input]) => urlOf(input));
    expect(urls).toContain("/api/v1/wealth/products");
    expect(urls).toContain("/api/v1/wealth/insurance");
  });

  it("compares a deposit and a term plan side by side with Not applicable cells", async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("checkbox", { name: regular.name }));
    expect(screen.queryByRole("table")).not.toBeInTheDocument(); // one product is not a comparison
    await user.click(screen.getByRole("checkbox", { name: term.name }));
    const table = screen.getByRole("table", { name: "Product comparison" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["Feature", regular.name, term.name]);
    expect(rowCells(table, "Yield")).toEqual(["7.10% p.a. (APY 7.29%)", NOT_APPLICABLE]);
    expect(rowCells(table, "Risk level")).toEqual(["Low", NOT_APPLICABLE]);
    expect(rowCells(table, "Minimum amount")).toEqual(["INR 10,000.00", NOT_APPLICABLE]);
    expect(rowCells(table, "Cover")).toEqual([
      NOT_APPLICABLE,
      "INR 25,00,000.00 to INR 5,00,00,000.00",
    ]);
    expect(rowCells(table, "Lock-in")).toEqual(["None", NOT_APPLICABLE]);
    expect(screen.getByRole("status")).toHaveTextContent("Comparing 2 products");
    // the scrollable wrapper is a named, focusable region so keyboard users can scroll at phone width
    expect(screen.getByRole("region", { name: "Product comparison" })).toHaveAttribute(
      "tabindex",
      "0",
    );
  });

  it("allows at most four products and lets one be removed to add another", async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    for (const name of [regular.name, elss.name, gold.name, term.name]) {
      await user.click(await screen.findByRole("checkbox", { name }));
    }
    const fifth = screen.getByRole("checkbox", { name: health.name });
    expect(fifth).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "You are comparing the maximum of 4 products. Remove one to add another.",
    );
    expect(within(screen.getByRole("table")).getAllByRole("columnheader")).toHaveLength(5);

    await user.click(screen.getByRole("checkbox", { name: elss.name })); // remove one
    expect(fifth).toBeEnabled();
    await user.click(fifth);
    const headers = within(screen.getByRole("table"))
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["Feature", regular.name, gold.name, term.name, health.name]);
  });

  it("shows an error state and retries", async () => {
    let attempts = 0;
    mockApi({
      insurance: () => {
        attempts += 1;
        return attempts === 1 ? json(500, { title: "x" }) : json(200, [term, health]);
      },
    });
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/Could not load the products/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("group", { name: "Insurance" })).toBeInTheDocument();
  });

  it("shows an empty state when neither catalogue has products", async () => {
    mockApi({ products: () => json(200, []), insurance: () => json(200, []) });
    renderPage();
    expect(await screen.findByText("There are no products to compare yet.")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
