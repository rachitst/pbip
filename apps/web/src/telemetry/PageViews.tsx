import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { matchRoutes, useLocation } from "react-router";
import type { RouteObject } from "react-router";
import { api } from "../api/client";
import { pageUrlAllowed } from "./catalogue";
import { browserAllowsAnalytics, clickstream } from "./tracker";

/** Shared with the Profile page's setting, so a change there reaches the tracker at once. */
export const ANALYTICS_CONSENT_KEY = ["analytics-consent"];

/** The route TEMPLATE a path matched ("/fraud/:alertId", never "/fraud/3f2c..."), built from the
 * route config, so ids in the path and the query string or fragment are never sent. A path that
 * only matches the catch-all ("*") is an unknown page and has no template: its raw text could be
 * anything a user typed, so it is not recorded. */
export function routeTemplate(routes: RouteObject[], pathname: string): string | null {
  const matches = matchRoutes(routes, pathname);
  if (!matches) return null;
  const segments: string[] = [];
  for (const { route } of matches) {
    if (route.path === undefined) continue;
    if (route.path.includes("*")) return null;
    segments.push(...route.path.split("/").filter(Boolean));
  }
  const template = `/${segments.join("/")}`;
  return pageUrlAllowed(template) ? template : null;
}

/** Mounted only inside the signed-in CUSTOMER area: collection starts with it and stops (dropping
 * anything unsent) when the customer signs out or moves to the staff area, which is never
 * instrumented. Records one page_view per visited route template. Nothing is sent unless the
 * customer's recorded analytics consent is on (ADR-0057); a failed lookup counts as refusal. */
export function ClickstreamPageViews({ routes }: { routes: RouteObject[] }) {
  const { pathname } = useLocation();
  const consent = useQuery({ queryKey: ANALYTICS_CONSENT_KEY, queryFn: api.analyticsConsent });
  useEffect(() => {
    clickstream.start();
    return () => {
      clickstream.stop();
    };
  }, []);
  const granted = consent.isError ? false : consent.data?.granted;
  useEffect(() => {
    if (granted !== undefined) clickstream.setConsent(granted && browserAllowsAnalytics());
  }, [granted]);
  useEffect(() => {
    const template = routeTemplate(routes, pathname);
    clickstream.setPage(template);
    if (template !== null) clickstream.track("page_view");
  }, [routes, pathname]);
  return null;
}
