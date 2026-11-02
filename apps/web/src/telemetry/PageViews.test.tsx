import { act, render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import type { RouteObject } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAccessToken } from "../api/client";
import { routes } from "../shell/routes";
import { pageUrlAllowed } from "./catalogue";
import { ClickstreamPageViews, routeTemplate } from "./PageViews";
import { FLUSH_INTERVAL_MS, clickstream } from "./tracker";

/** Every path pattern in the route config, joined to its full template. */
function allTemplates(config: RouteObject[], parent = ""): string[] {
  return config.flatMap((route) => {
    const path = route.path === undefined ? parent : `${parent}/${route.path}`.replace("//", "/");
    const own = route.path === undefined || route.path.includes("*") ? [] : [path];
    return [...own, ...allTemplates(route.children ?? [], path)];
  });
}

describe("route templates (TASK-112)", () => {
  it("records the route template, never ids, query strings or fragments", () => {
    expect(routeTemplate(routes, "/accounts")).toBe("/accounts");
    expect(routeTemplate(routes, "/insights")).toBe("/insights");
    expect(routeTemplate(routes, "/staff/fraud/3f2c9a2e-1111-4c1e-9d1e-2a2b2c2d2e2f")).toBe(
      "/staff/fraud/:alertId",
    );
  });

  it("has no template for an unknown page, whose path could be anything a user typed", () => {
    expect(routeTemplate(routes, "/alice@example.com")).toBeNull();
    expect(routeTemplate(routes, "/accounts/409443369528/extra")).toBeNull();
  });

  it("every route template is a page the ingestion API accepts", () => {
    const templates = allTemplates(routes);
    expect(templates).toContain("/accounts");
    expect(templates).toContain("/staff/fraud/:alertId");
    for (const template of templates) expect(pageUrlAllowed(template)).toBe(true);
  });
});

describe("ClickstreamPageViews", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const app: RouteObject[] = [
    {
      element: <ClickstreamPageViews routes={[{ path: "things/:thingId" }, { path: "list" }]} />,
      children: [{ path: "*" }],
    },
  ];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("token");
  });
  afterEach(() => {
    setAccessToken(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("sends one page_view per visited template and stops collecting when unmounted", async () => {
    const router = createMemoryRouter(app, { initialEntries: ["/things/42?email=a@b.c#x"] });
    const { unmount } = render(<RouterProvider router={router} />);
    await act(async () => {
      await router.navigate("/list");
    });
    await act(async () => {
      await router.navigate("/nowhere/alice");
    });
    act(() => {
      vi.advanceTimersByTime(FLUSH_INTERVAL_MS);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string) as {
      events: { event_type: string; page_url?: string }[];
    };
    expect(body.events.map((e) => [e.event_type, e.page_url])).toEqual([
      ["page_view", "/things/:thingId"],
      ["page_view", "/list"],
    ]);

    unmount();
    clickstream.track("page_view");
    expect(clickstream.pending()).toEqual([]);
  });
});
