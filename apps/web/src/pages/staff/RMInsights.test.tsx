import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  CustomerInsightDetail,
  CustomerInsightItem,
  CustomerInsightsListResponse,
  InteractionListResponse,
  InteractionOut,
  UserOut,
} from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { RMInsights } from "./RMInsights";
import { canAccessRMInsights, declineText, inactivityText, segmentLabel } from "./rmInsightsFormat";

const rm: UserOut = {
  id: "u-rm",
  email: "rm@demo.pbip",
  roles: ["RELATIONSHIP_MANAGER"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};
const admin: UserOut = { ...rm, id: "u-admin", roles: ["ADMIN"] };
const analyst: UserOut = { ...rm, id: "u-fa", roles: ["FRAUD_ANALYST"] };
const compliance: UserOut = { ...rm, id: "u-co", roles: ["COMPLIANCE_OFFICER"] };

const ASHA = "c0ffee00-1111-4222-8333-000000000001";
const RAVI = "c0ffee00-1111-4222-8333-000000000002";

const asha: CustomerInsightItem = {
  customer_id: ASHA,
  full_name: "Asha Rao",
  segment: "INACTIVE_SAVER",
  total_balance_minor: 150_000_00,
  inactivity_days: 45,
  balance_decline_rate: 0.4,
  churn_risk_score: 0.82,
  churn_risk_level: "HIGH",
  top_recommended_product: "Fixed deposit",
  top_product_score: 0.91,
  loan_product: "Personal credit line",
  loan_propensity_score: 0.64,
  high_value: true,
};
const ravi: CustomerInsightItem = {
  ...asha,
  customer_id: RAVI,
  full_name: "Ravi Kumar",
  segment: "UNSEGMENTED",
  total_balance_minor: 2_500_00,
  inactivity_days: 0,
  balance_decline_rate: 0,
  churn_risk_score: 0.1,
  churn_risk_level: "LOW",
  loan_product: null,
  loan_propensity_score: null,
  high_value: false,
};

function page(items: CustomerInsightItem[], total = items.length): CustomerInsightsListResponse {
  return { items, total, evaluated_at: "2026-11-01T12:00:00Z" };
}

const detail: CustomerInsightDetail = {
  insight: asha,
  customer_since: "2025-03-14T09:00:00Z",
  health: { overall_score: 58, rating: "FAIR", insufficient_data: false },
  transaction_volume: {
    window_days: 30,
    transaction_count: 2,
    inflow_minor: 100_000_00,
    outflow_minor: 40_000_00,
  },
  recommendations: [
    {
      title: "Fixed deposit",
      product_type: "SAVINGS",
      score: 0.91,
      reasons: ["Your financial health score is 58/100."],
    },
  ],
  evaluated_at: "2026-11-01T12:00:00Z",
};

const logged: InteractionOut = {
  id: "i-1",
  channel: "CALL",
  outcome: "FOLLOW_UP",
  note: "Asked about a credit line.",
  logged_by: "rm@demo.pbip",
  created_at: "2026-11-01T11:00:00Z",
};

interface Routes {
  list?: () => Response;
  detail?: () => Response;
  log?: () => Response;
  post?: (body: unknown) => Response;
}

/** Answers the insights endpoints by path and method; records every call for assertions. */
function mockApi(routes: Routes) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(urlOf(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname.endsWith("/interactions")) {
      if (method === "POST") {
        const body: unknown = JSON.parse(typeof init?.body === "string" ? init.body : "null");
        return Promise.resolve(routes.post ? routes.post(body) : json(500, {}));
      }
      return Promise.resolve(routes.log ? routes.log() : json(200, { items: [], total: 0 }));
    }
    if (url.pathname === "/api/v1/insights/customers") {
      return Promise.resolve(routes.list ? routes.list() : json(200, page([])));
    }
    return Promise.resolve(routes.detail ? routes.detail() : json(200, detail));
  });
}

function listCalls(fetchMock: ReturnType<typeof mockApi>): URLSearchParams[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(urlOf(input), "http://localhost"))
    .filter((u) => u.pathname === "/api/v1/insights/customers")
    .map((u) => u.searchParams);
}

function renderPage(me: UserOut = rm) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>
          <RMInsights />
        </MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("rmInsightsFormat", () => {
  it("admits relationship managers and admins only", () => {
    expect(canAccessRMInsights(rm)).toBe(true);
    expect(canAccessRMInsights(admin)).toBe(true);
    expect(canAccessRMInsights(analyst)).toBe(false);
    expect(canAccessRMInsights(compliance)).toBe(false);
  });

  it("reads segments, inactivity and decline as words", () => {
    expect(segmentLabel("INACTIVE_SAVER")).toBe("Inactive saver");
    expect(segmentLabel("UNSEGMENTED")).toBe("Not segmented yet");
    expect(segmentLabel("NEW_SEGMENT_X")).toBe("New segment x");
    expect(inactivityText(0)).toBe("Active today");
    expect(inactivityText(1)).toBe("1 day");
    expect(inactivityText(45)).toBe("45 days");
    expect(declineText(0)).toBe("No decline");
    expect(declineText(0.404)).toBe("40%");
  });
});

describe("RMInsights page (TASK-066)", () => {
  it.each([analyst, compliance])("blocks $roles and never calls the API", (me) => {
    const fetchMock = mockApi({});
    renderPage(me);
    expect(screen.getByText(/Access restricted/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the leads with readable values from the API", async () => {
    mockApi({ list: () => json(200, page([asha, ravi])) });
    renderPage();
    const table = await screen.findByRole("table");
    const [, ashaRow, raviRow] = within(table).getAllByRole("row");
    if (!ashaRow || !raviRow) throw new Error("rows missing");
    expect(within(ashaRow).getByText("Asha Rao")).toBeInTheDocument();
    expect(within(ashaRow).getByText("High value")).toBeInTheDocument();
    expect(within(ashaRow).getByText("Inactive saver")).toBeInTheDocument();
    expect(within(ashaRow).getByText("INR 1,50,000.00")).toBeInTheDocument();
    expect(within(ashaRow).getByText("High · 82")).toBeInTheDocument();
    expect(within(ashaRow).getByText("45 days ago")).toBeInTheDocument();
    expect(within(ashaRow).getByText("(Personal credit line)")).toBeInTheDocument();
    expect(within(raviRow).getByText("Not segmented yet")).toBeInTheDocument();
    expect(within(raviRow).getByText("Today")).toBeInTheDocument();
    expect(within(raviRow).getByText("No eligible loan")).toBeInTheDocument();
    expect(screen.getByText(/Showing 1-2 of 2 customers/)).toBeInTheDocument();
  });

  it("says 1 customer, not 1 customers", async () => {
    mockApi({ list: () => json(200, page([asha])) });
    renderPage(admin);
    expect(await screen.findByText(/Showing 1-1 of 1 customer\./)).toBeInTheDocument();
  });

  it("sends every filter to the API and starts again from the first page", async () => {
    const user = userEvent.setup();
    const many = page([asha, ravi], 45);
    const fetchMock = mockApi({ list: () => json(200, many) });
    renderPage();
    await screen.findByRole("table");
    expect(listCalls(fetchMock)[0]?.toString()).toBe("limit=20&offset=0");

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => {
      expect(listCalls(fetchMock).at(-1)?.get("offset")).toBe("20");
    });

    await user.selectOptions(screen.getByLabelText("Segment"), "INACTIVE_SAVER");
    await user.selectOptions(screen.getByLabelText("Churn risk"), "0.7");
    await user.selectOptions(screen.getByLabelText("Loan propensity"), "0.5");
    await waitFor(() => {
      const last = listCalls(fetchMock).at(-1);
      expect(last?.get("segment")).toBe("INACTIVE_SAVER");
      expect(last?.get("min_churn_risk")).toBe("0.7");
      expect(last?.get("min_loan_propensity")).toBe("0.5");
      expect(last?.get("offset")).toBe("0");
    });
  });

  it("disables Previous on the first page and Next on the last", async () => {
    mockApi({ list: () => json(200, page([asha, ravi])) });
    renderPage();
    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("explains an empty result, and an error with a retry", async () => {
    const user = userEvent.setup();
    let fail = true;
    mockApi({
      list: () =>
        fail ? json(500, { title: "Internal Server Error", status: 500 }) : json(200, page([])),
    });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load customer insights.");
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("There are no customers yet.")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Segment"), "HIGH_NET_WORTH");
    expect(await screen.findByText("No customers match these filters.")).toBeInTheDocument();
  });
});

describe("customer flyout (TASK-066)", () => {
  async function openAsha(routes: Routes) {
    const user = userEvent.setup();
    const fetchMock = mockApi({ list: () => json(200, page([asha])), ...routes });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Open Asha Rao" }));
    const dialog = await screen.findByRole("dialog", { name: "Asha Rao" });
    return { user, fetchMock, dialog };
  }

  it("loads the detail for that customer and shows volume, health and churn", async () => {
    const { fetchMock, dialog } = await openAsha({});
    expect(await within(dialog).findByText("2 transactions")).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([input]) => urlOf(input));
    expect(urls).toContain(`/api/v1/insights/customers/${ASHA}`);
    expect(within(dialog).getByText("INR 1,00,000.00")).toBeInTheDocument();
    expect(within(dialog).getByText("INR 40,000.00")).toBeInTheDocument();
    expect(within(dialog).getByText("58 out of 100")).toBeInTheDocument();
    expect(within(dialog).getByText("Fair")).toBeInTheDocument();
    expect(within(dialog).getByText("82 out of 100")).toBeInTheDocument();
    expect(within(dialog).getByText(/rule-based score, not a measured probability/)).toBeVisible();
    expect(within(dialog).getByText("40%")).toBeInTheDocument();
    expect(within(dialog).getByText("Your financial health score is 58/100.")).toBeInTheDocument();
    expect(within(dialog).getByText(/customer since 14 Mar 2025/)).toBeInTheDocument();
  });

  it("says 1 transaction and handles a customer with no account", async () => {
    const one: CustomerInsightDetail = {
      ...detail,
      health: null,
      recommendations: [],
      transaction_volume: { ...detail.transaction_volume, transaction_count: 1 },
    };
    const { dialog } = await openAsha({ detail: () => json(200, one) });
    expect(await within(dialog).findByText("1 transaction")).toBeInTheDocument();
    expect(within(dialog).getByText(/no health score/)).toBeInTheDocument();
    expect(within(dialog).getByText(/No product passes the eligibility rules/)).toBeVisible();
  });

  it("shows an error when the detail cannot load", async () => {
    const { dialog } = await openAsha({ detail: () => json(404, { status: 404 }) });
    expect(
      await within(dialog).findByText(/Could not load this customer|not found/i),
    ).toBeVisible();
  });

  it("refuses an empty note without calling the API", async () => {
    const post = vi.fn(() => json(201, logged));
    const { user, dialog } = await openAsha({ post });
    expect(await within(dialog).findByText("Nothing logged for this customer yet.")).toBeVisible();
    await user.type(within(dialog).getByLabelText(/^Note/), "   ");
    await user.click(within(dialog).getByRole("button", { name: "Log it" }));
    expect(within(dialog).getByText("Write what was discussed.")).toBeVisible();
    expect(post).not.toHaveBeenCalled();
  });

  it("posts the note to the API and shows the saved log", async () => {
    let saved: InteractionListResponse = { items: [], total: 0 };
    const post = vi.fn((body: unknown) => {
      saved = { items: [logged], total: 1 };
      return json(201, { ...logged, ...(body as object) });
    });
    const { user, dialog } = await openAsha({ post, log: () => json(200, saved) });
    await within(dialog).findByText("Nothing logged for this customer yet.");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "MEETING");
    await user.selectOptions(within(dialog).getByLabelText("Outcome"), "INTERESTED");
    await user.type(within(dialog).getByLabelText(/^Note/), "  Asked about a credit line.  ");
    await user.click(within(dialog).getByRole("button", { name: "Log it" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith({
        channel: "MEETING",
        outcome: "INTERESTED",
        note: "Asked about a credit line.",
      });
    });
    expect(await within(dialog).findByText("Asked about a credit line.")).toBeVisible();
    expect(within(dialog).getByText("1 entry")).toBeVisible();
    expect(within(dialog).getByText(/rm@demo\.pbip/)).toBeVisible();
    expect(within(dialog).getByLabelText(/^Note/)).toHaveValue("");
    expect(localStorage.length).toBe(0); // nothing about the customer is kept in the browser
  });

  it("keeps the note and shows the error when saving fails", async () => {
    const post = vi.fn(() => json(403, { status: 403, title: "Forbidden" }));
    const { user, dialog } = await openAsha({ post });
    await user.type(within(dialog).getByLabelText(/^Note/), "Left a voicemail.");
    await user.click(within(dialog).getByRole("button", { name: "Log it" }));
    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(within(dialog).getByLabelText(/^Note/)).toHaveValue("Left a voicemail.");
  });

  it("closes with the Close button", async () => {
    const { user, dialog } = await openAsha({});
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});
