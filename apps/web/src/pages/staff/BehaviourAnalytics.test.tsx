import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BehaviourAnalytics as Analytics, UserOut } from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import BehaviourAnalytics, {
  AffinityPanel,
  EngagementPanel,
  SegmentsPanel,
  SpendingPanel,
  sessionLength,
} from "./BehaviourAnalytics";

const rm: UserOut = {
  id: "u-rm",
  email: "rm@demo.pbip",
  roles: ["RELATIONSHIP_MANAGER"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const SEGMENTS = [
  "HIGH_NET_WORTH",
  "BUDGET_CONSCIOUS",
  "FREQUENT_TRANSACTOR",
  "DISCRETIONARY_SPENDER",
  "INACTIVE_SAVER",
  "UNSEGMENTED",
] as const;

const emptyItem = {
  customers: 0,
  share_pct: 0,
  avg_recency_days: null,
  avg_monthly_transactions: null,
  avg_monthly_spend_minor: null,
  avg_savings_ratio: null,
  avg_discretionary_ratio: null,
};

const empty: Analytics = {
  generated_at: new Date().toISOString(),
  period_days: 30,
  period_start: "2026-10-03T00:00:00Z",
  period_end: "2026-11-02T00:00:00Z",
  segment: null,
  currency: "INR",
  segments: {
    total_customers: 0,
    last_segmented_at: null,
    items: SEGMENTS.map((segment) => ({ segment, ...emptyItem })),
  },
  spending: [],
  affinity: {
    products: ["SAVINGS_ACCOUNT"],
    segments: [],
    recommendation_clicks: [],
    profiled_customers: 0,
    interests: [],
  },
  engagement: {
    events: 0,
    sessions: 0,
    active_customers: 0,
    customers: 0,
    consenting_customers: 0,
    active_share_pct: 0,
    events_per_session: 0,
    avg_session_seconds: 0,
    by_event_type: [],
    by_segment: [],
    daily: [],
  },
};

const full: Analytics = {
  ...empty,
  segments: {
    total_customers: 4,
    last_segmented_at: new Date().toISOString(),
    items: SEGMENTS.map((segment) =>
      segment === "HIGH_NET_WORTH"
        ? {
            ...emptyItem,
            segment,
            customers: 2,
            share_pct: 50,
            avg_monthly_spend_minor: 40_000_00,
            avg_monthly_transactions: 4,
            avg_savings_ratio: 0.25,
          }
        : { segment, ...emptyItem },
    ),
  },
  spending: [
    {
      segment: "HIGH_NET_WORTH",
      customers: 2,
      spenders: 2,
      spend_minor: 22_000,
      transactions: 3,
      avg_spend_per_spender_minor: 11_000,
      by_type: [{ transaction_type: "TRANSFER", spend_minor: 22_000, transactions: 3 }],
    },
  ],
  affinity: {
    products: ["SAVINGS_ACCOUNT", "PERSONAL_LOAN"],
    segments: [
      {
        segment: "HIGH_NET_WORTH",
        customers: 2,
        holdings: [
          { product: "SAVINGS_ACCOUNT", holders: 2, share_pct: 100 },
          { product: "PERSONAL_LOAN", holders: 1, share_pct: 50 },
        ],
      },
    ],
    recommendation_clicks: [
      {
        product_id: "00000000-0000-4000-a000-000000000002",
        title: "Fixed deposit",
        product_type: "SAVINGS",
        clicks: 3,
        customers: 2,
      },
    ],
    profiled_customers: 0,
    interests: [],
  },
  engagement: {
    events: 6,
    sessions: 3,
    active_customers: 3,
    customers: 4,
    consenting_customers: 3,
    active_share_pct: 75,
    events_per_session: 2,
    avg_session_seconds: 50,
    by_event_type: [{ event_type: "page_view", events: 2 }],
    by_segment: [
      { segment: "HIGH_NET_WORTH", customers: 2, active_customers: 1, active_share_pct: 50 },
    ],
    daily: [
      { day: "2026-10-31", active_customers: 3, events: 6, sessions: 3, avg_session_seconds: 50 },
    ],
  },
};

function renderWith(ui: ReactElement, me: UserOut = rm) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>{ui}</MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("sessionLength", () => {
  it("keeps the seconds", () => {
    expect(sessionLength(0)).toBe("0 s");
    expect(sessionLength(50)).toBe("50 s");
    expect(sessionLength(125)).toBe("2 min 5 s");
  });
});

describe("Segment mix panel", () => {
  it("has a text summary of the chart, a table and per-segment drill-down for RMs", () => {
    renderWith(<SegmentsPanel panel={full.segments} canDrill />);
    expect(
      screen.getByRole("img", { name: /High net worth 2, Budget conscious 0/ }),
    ).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /High net worth/ });
    expect(within(row).getByText("50.0%")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "View 2 customers" })).toHaveAttribute(
      "href",
      "/staff/insights?segment=HIGH_NET_WORTH",
    );
    expect(
      within(screen.getByRole("row", { name: /Inactive saver/ })).getByText("None"),
    ).toBeVisible();
  });

  it("has no drill-down without the RM role, and an empty state", () => {
    const { unmount } = renderWith(<SegmentsPanel panel={full.segments} canDrill={false} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    unmount();
    renderWith(<SegmentsPanel panel={empty.segments} canDrill />);
    expect(screen.getByText("There are no customers yet.")).toBeInTheDocument();
    expect(screen.getByText(/has not run yet/)).toBeInTheDocument();
  });
});

describe("Spending, affinity and engagement panels", () => {
  it("lists spend per segment with its definition", () => {
    renderWith(<SpendingPanel rows={full.spending} currency="INR" period="last 30 days" />);
    const row = screen.getByRole("row", { name: /High net worth/ });
    expect(within(row).getByText("Transfers: 3")).toBeInTheDocument();
    expect(
      screen.getByText(/excluding reversals and moves between their own accounts/),
    ).toBeVisible();
  });

  it("shows an honest empty state when nobody spent", () => {
    renderWith(<SpendingPanel rows={[]} currency="INR" period="last 30 days" />);
    expect(screen.getByText("No customer spending in the last 30 days.")).toBeInTheDocument();
  });

  it("shows holdings shares and recommendation clicks", () => {
    renderWith(<AffinityPanel panel={full.affinity} period="last 30 days" />);
    const row = screen.getByRole("row", { name: /High net worth/ });
    expect(within(row).getByText("100.0%")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Personal loan" })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /Fixed deposit 3 2/ })).toBeInTheDocument();
  });

  it("shows engagement metrics with a chart summary and a day table", () => {
    renderWith(<EngagementPanel panel={full.engagement} period="last 30 days" />);
    expect(screen.getByText("3 of 4")).toBeInTheDocument();
    expect(screen.getByText("50 s")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Active customers per day: 2026-10-31 3." }),
    ).toBeVisible();
    expect(screen.getByRole("row", { name: "2026-10-31 3 6" })).toBeInTheDocument();
  });

  it("explains an empty clickstream instead of showing zeros as a chart", () => {
    renderWith(<EngagementPanel panel={empty.engagement} period="last 30 days" />);
    expect(screen.getByText(/No clickstream events in the last 30 days/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});

describe("BehaviourAnalytics page (TASK-101)", () => {
  it("loads, then refetches with the segment and period filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, full));
    const user = userEvent.setup();
    renderWith(<BehaviourAnalytics />);
    expect(screen.getByText("Loading behavioural analytics...")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Segment mix" })).toBeVisible();
    for (const name of ["Spending patterns", "Product affinity", "Engagement"]) {
      expect(screen.getByRole("heading", { name })).toBeVisible();
    }
    expect(urlOf(fetchMock.mock.calls[0]?.[0] ?? "")).toContain("/insights/behaviour?days=30");
    await user.selectOptions(screen.getByLabelText("Segment"), "BUDGET_CONSCIOUS");
    await user.selectOptions(screen.getByLabelText("Period"), "180");
    await waitFor(() => {
      expect(urlOf(fetchMock.mock.calls.at(-1)?.[0] ?? "")).toContain(
        "days=180&segment=BUDGET_CONSCIOUS",
      );
    });
  });

  it("shows an error with retry", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(json(500, { title: "boom" }))
      .mockResolvedValue(json(200, empty));
    const user = userEvent.setup();
    renderWith(<BehaviourAnalytics />);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("There are no customers yet.")).toBeInTheDocument();
  });

  it("does not call the API without the RM or admin role", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderWith(<BehaviourAnalytics />, { ...rm, roles: ["COMPLIANCE_OFFICER"] });
    expect(screen.getByText(/need the Relationship Manager or Administrator role/)).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
