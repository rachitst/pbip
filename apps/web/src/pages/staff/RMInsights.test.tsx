import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  BehaviourProfileResponse,
  CustomerInsightDetail,
  CustomerInsightItem,
  CustomerInsightsListResponse,
  InteractionListResponse,
  InteractionOut,
  UserOut,
} from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, noContent, urlOf } from "../../test-utils";
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
  top_product_reason: "You keep 4.0 months of spending as a buffer.",
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
  top_recommended_product: null,
  top_product_score: null,
  top_product_reason: null,
  loan_product: null,
  loan_propensity_score: null,
  high_value: false,
};

function page(
  items: CustomerInsightItem[],
  total = items.length,
  scope: CustomerInsightsListResponse["scope"] = "ASSIGNED",
  order: CustomerInsightsListResponse["order"] = "PROPENSITY",
): CustomerInsightsListResponse {
  return {
    items,
    total,
    evaluated_at: "2026-11-01T12:00:00Z",
    scope,
    order,
    product_score_version: "rules-v1",
  };
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
  assigned_rm: null,
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
  rms?: () => Response;
  assign?: (method: string, body: unknown) => Response;
  profile?: () => Response;
}

/** Answers the insights endpoints by path and method; records every call for assertions. */
function mockApi(routes: Routes) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = new URL(urlOf(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/v1/insights/relationship-managers") {
      return Promise.resolve(routes.rms ? routes.rms() : json(200, { items: [] }));
    }
    if (url.pathname.endsWith("/assignment")) {
      const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      return Promise.resolve(routes.assign ? routes.assign(method, body) : json(500, {}));
    }
    if (url.pathname.endsWith("/behaviour-profile")) {
      return Promise.resolve(
        routes.profile ? routes.profile() : json(200, { analytics_consent: false, profile: null }),
      );
    }
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

function renderPage(me: UserOut = rm, path = "/staff/insights") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter initialEntries={[path]}>
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
  it("starts filtered by a known segment from the behavioural dashboard link (TASK-101)", async () => {
    const fetchMock = mockApi({});
    renderPage(rm, "/staff/insights?segment=BUDGET_CONSCIOUS");
    await waitFor(() => {
      expect(listCalls(fetchMock)[0]?.get("segment")).toBe("BUDGET_CONSCIOUS");
    });
    expect(screen.getByLabelText("Segment")).toHaveValue("BUDGET_CONSCIOUS");
  });

  it("ignores an unknown segment in the link", async () => {
    const fetchMock = mockApi({});
    renderPage(rm, "/staff/insights?segment=<script>");
    await waitFor(() => {
      expect(listCalls(fetchMock)).toHaveLength(1);
    });
    expect(listCalls(fetchMock)[0]?.get("segment")).toBeNull();
  });

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

  // jsdom has no layout, so this cannot prove the page does not scroll sideways at 360px (that needs
  // a browser). It pins the cause: an absolutely positioned sr-only span in the header escaped the
  // table's scroll region and widened the page, so the Details header must be plain visible text.
  it("labels the last column with a visible Details header, not an sr-only span", async () => {
    mockApi({ list: () => json(200, page([asha])) });
    renderPage();
    const table = await screen.findByRole("table");
    const headers = within(table).getAllByRole("columnheader");
    const last = headers[headers.length - 1];
    if (!last) throw new Error("no column headers");
    expect(last).toHaveTextContent("Details");
    expect(last.querySelector(".sr-only")).toBeNull();
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
    expect(listCalls(fetchMock)[0]?.toString()).toBe("limit=20&offset=0&order_by=PROPENSITY");

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

  it("orders an RM's list by product match and shows the rule-based score and reason (TASK-056)", async () => {
    const fetchMock = mockApi({ list: () => json(200, page([asha, ravi])) });
    renderPage(rm);
    const table = await screen.findByRole("table");
    expect(listCalls(fetchMock)[0]?.get("order_by")).toBe("PROPENSITY");
    expect(screen.getByLabelText("Order")).toHaveValue("PROPENSITY");
    expect(within(table).getByText("Customers, best product match first")).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: "Product match (rule-based)" }),
    ).toBeInTheDocument();
    const [, ashaRow, raviRow] = within(table).getAllByRole("row");
    if (!ashaRow || !raviRow) throw new Error("rows missing");
    expect(within(ashaRow).getByText("(Fixed deposit)")).toBeInTheDocument();
    expect(
      within(ashaRow).getByText("You keep 4.0 months of spending as a buffer."),
    ).toBeInTheDocument();
    expect(within(ashaRow).getByText("High · 82")).toBeInTheDocument(); // churn stays visible
    expect(within(raviRow).getByText("No eligible product")).toBeInTheDocument();
    expect(screen.getByText(/rule-based score \(not a trained model/)).toBeInTheDocument();
  });

  it("starts an administrator on churn risk and switches the order from the first page", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi({
      list: () => json(200, page([asha, ravi], 45, "ALL", "CHURN_RISK")),
    });
    renderPage(admin);
    const table = await screen.findByRole("table");
    expect(listCalls(fetchMock)[0]?.get("order_by")).toBe("CHURN_RISK");
    expect(within(table).getByText("Customers, highest churn risk first")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => {
      expect(listCalls(fetchMock).at(-1)?.get("offset")).toBe("20");
    });
    await user.selectOptions(screen.getByLabelText("Order"), "PROPENSITY");
    await waitFor(() => {
      const last = listCalls(fetchMock).at(-1);
      expect(last?.get("order_by")).toBe("PROPENSITY");
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
        fail
          ? json(500, { title: "Internal Server Error", status: 500 })
          : json(200, page([], 0, "ALL")),
    });
    renderPage(admin);
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

  it("shows the behavioural profile when the customer turned analytics on (ADR-0057)", async () => {
    const profile: BehaviourProfileResponse = {
      analytics_consent: true,
      profile: {
        computed_at: "2026-11-04T03:40:00Z",
        segment: "HIGH_NET_WORTH",
        spend_income: {
          currency: "INR",
          monthly_income_minor: 75_000_00,
          monthly_spend_minor: 32_000_00,
          discretionary_ratio: 0.3,
        },
        engagement: {
          window_days: 90,
          sessions: 3,
          avg_session_seconds: 125,
          pages_per_session: 1.67,
          interactions: 9,
          last_active_at: "2026-11-03T10:00:00Z",
        },
        product_interests: [
          { category: "LOAN", score: 5.434, signals: 3, last_signal_at: "2026-11-02T10:02:00Z" },
        ],
      },
    };
    const { dialog, fetchMock } = await openAsha({ profile: () => json(200, profile) });
    const section = await within(dialog).findByRole("region", { name: "Behavioural profile" });
    expect(await within(section).findByText("2 min 5 s")).toBeInTheDocument();
    expect(within(section).getByText("INR 75,000.00")).toBeInTheDocument();
    expect(within(section).getByText("1.67")).toBeInTheDocument();
    expect(within(section).getByText("Loan or credit")).toBeInTheDocument();
    expect(within(section).getByText(/score 5\.43 · 3 signals/)).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([input]) => urlOf(input));
    expect(urls).toContain(`/api/v1/insights/customers/${ASHA}/behaviour-profile`);
  });

  it("says there is no behavioural profile without the customer's consent", async () => {
    const { dialog } = await openAsha({});
    const section = await within(dialog).findByRole("region", { name: "Behavioural profile" });
    expect(
      await within(section).findByText(/has not turned on usage analytics/),
    ).toBeInTheDocument();
    expect(within(section).queryByText("Product interests")).toBeNull();
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

describe("RM portfolio scoping and assignment (ADR-0032)", () => {
  const OTHER_RM = { user_id: "u-rm-2", email: "rm2@bank.example" };
  const RMS = { items: [{ user_id: rm.id, email: rm.email }, OTHER_RM] };

  async function openAshaAs(me: UserOut, routes: Routes) {
    const user = userEvent.setup();
    const fetchMock = mockApi({
      list: () => json(200, page([asha], 1, me === admin ? "ALL" : "ASSIGNED")),
      ...routes,
    });
    renderPage(me);
    await user.click(await screen.findByRole("button", { name: "Open Asha Rao" }));
    const dialog = await screen.findByRole("dialog", { name: "Asha Rao" });
    return { user, fetchMock, dialog };
  }

  function calls(fetchMock: ReturnType<typeof mockApi>, suffix: string) {
    return fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith(suffix));
  }

  it("tells an RM the list is their own customers, and why it may be empty", async () => {
    mockApi({ list: () => json(200, page([])) });
    renderPage(rm);
    expect(await screen.findByTestId("rm-scope")).toHaveTextContent(
      "Your customers: only customers assigned to you are listed.",
    );
    expect(screen.getByText(/No customers are assigned to you yet/)).toBeInTheDocument();
  });

  it("tells an administrator the list is every customer", async () => {
    mockApi({ list: () => json(200, page([asha], 1, "ALL")) });
    renderPage(admin);
    expect(await screen.findByTestId("rm-scope")).toHaveTextContent(/All customers/);
  });

  it("shows an RM who looks after the customer but no assignment control", async () => {
    const assigned = { rm_user_id: rm.id, rm_email: rm.email, assigned_at: "2026-10-01T09:00:00Z" };
    const { fetchMock, dialog } = await openAshaAs(rm, {
      detail: () => json(200, { ...detail, assigned_rm: assigned }),
    });
    expect(await within(dialog).findByText("rm@demo.pbip")).toBeVisible();
    expect(within(dialog).getByText(/since 1 Oct 2026/)).toBeVisible();
    expect(within(dialog).queryByLabelText("Assign to")).not.toBeInTheDocument();
    expect(calls(fetchMock, "/relationship-managers")).toHaveLength(0);
  });

  it("lets an administrator assign an unassigned customer", async () => {
    let current: CustomerInsightDetail = detail;
    const assign = vi.fn((method: string, body: unknown) => {
      const chosen = body as { rm_user_id: string };
      const rmAssigned = {
        rm_user_id: chosen.rm_user_id,
        rm_email: OTHER_RM.email,
        assigned_at: "2026-11-03T09:00:00Z",
      };
      current = { ...detail, assigned_rm: rmAssigned };
      return json(200, { customer_id: ASHA, assigned_rm: rmAssigned, method });
    });
    const { user, dialog } = await openAshaAs(admin, {
      detail: () => json(200, current),
      rms: () => json(200, RMS),
      assign,
    });
    expect(await within(dialog).findByText(/Unassigned/)).toBeVisible();
    const assignButton = await within(dialog).findByRole("button", { name: "Assign" });
    expect(assignButton).toBeDisabled(); // nothing chosen yet
    await user.selectOptions(within(dialog).getByLabelText("Assign to"), OTHER_RM.user_id);
    await user.click(assignButton);
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith("PUT", { rm_user_id: OTHER_RM.user_id });
    });
    expect(await within(dialog).findByText(OTHER_RM.email, { selector: "strong" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Unassign" })).toBeVisible();
  });

  it("lets an administrator unassign, and shows a failure", async () => {
    const assigned = { rm_user_id: rm.id, rm_email: rm.email, assigned_at: "2026-10-01T09:00:00Z" };
    let fail = true;
    const assign = vi.fn((method: string) =>
      fail
        ? json(403, { status: 403, title: "Forbidden" })
        : method === "DELETE"
          ? noContent()
          : json(200, {}),
    );
    const { user, dialog } = await openAshaAs(admin, {
      detail: () => json(200, { ...detail, assigned_rm: assigned }),
      rms: () => json(200, RMS),
      assign,
    });
    const unassign = await within(dialog).findByRole("button", { name: "Unassign" });
    expect(within(dialog).getByRole("button", { name: "Reassign" })).toBeDisabled(); // same RM
    await user.click(unassign);
    expect(
      await within(dialog).findByText(/Could not change the assignment|Forbidden/),
    ).toBeVisible();
    fail = false;
    await user.click(unassign);
    await waitFor(() => {
      expect(assign).toHaveBeenLastCalledWith("DELETE", null);
    });
  });
});
