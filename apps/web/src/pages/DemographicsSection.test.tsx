import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DemographicsOut } from "../api/client";
import { json, noContent, urlOf } from "../test-utils";
import { DemographicsSection } from "./DemographicsSection";

const nothing: DemographicsOut = {
  date_of_birth: null,
  age_band: "unknown",
  recommendations_consent: false,
  consent_granted_at: null,
  consent_policy_version: null,
  current_policy_version: "ADR-0015-v1",
};

function renderSection() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DemographicsSection />
    </QueryClientProvider>,
  );
}

describe("date of birth and recommendations consent", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let current: DemographicsOut = nothing;
  let failGet = false;
  let rejectDate = false;

  beforeEach(() => {
    current = nothing;
    failGet = false;
    rejectDate = false;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/customers/me/demographics")) {
        if (failGet) return Promise.resolve(json(500, { code: "error", title: "boom" }));
        return Promise.resolve(json(200, current));
      }
      if (url.endsWith("/customers/me/date-of-birth") && method === "PUT") {
        if (rejectDate) {
          return Promise.resolve(
            json(422, {
              code: "unprocessable",
              title: "date of birth must give an age between 18 and 120",
            }),
          );
        }
        const { date_of_birth } = JSON.parse(init?.body as string) as { date_of_birth: string };
        current = { ...current, date_of_birth, age_band: "60+" };
        return Promise.resolve(json(200, current));
      }
      if (url.endsWith("/customers/me/date-of-birth") && method === "DELETE") {
        current = { ...current, date_of_birth: null, age_band: "unknown" };
        return Promise.resolve(noContent());
      }
      if (url.endsWith("/customers/me/recommendations-consent")) {
        const { granted } = JSON.parse(init?.body as string) as { granted: boolean };
        current = {
          ...current,
          recommendations_consent: granted,
          consent_granted_at: granted ? "2026-11-02T06:30:00Z" : null,
          consent_policy_version: granted ? "ADR-0015-v1" : null,
        };
        return Promise.resolve(json(200, current));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const sent = (path: string, method: string) =>
    fetchMock.mock.calls.filter(
      ([input, init]) => urlOf(input).endsWith(path) && init?.method === method,
    );

  it("shows loading, then an empty profile with consent off by default", async () => {
    renderSection();
    expect(screen.getByRole("status")).toHaveTextContent("Loading your date of birth");
    expect(await screen.findByText("Not provided")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Give consent" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete date of birth" })).toBeNull();
  });

  it("shows an error state with a retry", async () => {
    failGet = true;
    const user = userEvent.setup();
    renderSection();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your date of birth");
    failGet = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Not provided")).toBeInTheDocument();
  });

  it("saves a date of birth as an ISO date and shows the derived band", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.type(await screen.findByLabelText("Date of birth"), "1958-04-17");
    await user.click(screen.getByRole("button", { name: "Save date of birth" }));
    expect(await screen.findByText("Date of birth saved.")).toBeInTheDocument();
    expect(screen.getByText("17 April 1958")).toBeInTheDocument();
    expect(screen.getByText("60 or over")).toBeInTheDocument();
    const body = sent("/customers/me/date-of-birth", "PUT")[0]?.[1]?.body as string;
    expect(JSON.parse(body)).toEqual({ date_of_birth: "1958-04-17" });
  });

  it("shows the server's age-limit message when a date is refused", async () => {
    rejectDate = true;
    const user = userEvent.setup();
    renderSection();
    await user.type(await screen.findByLabelText("Date of birth"), "2015-01-01");
    await user.click(screen.getByRole("button", { name: "Save date of birth" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("between 18 and 120");
    expect(screen.getByText("Not provided")).toBeInTheDocument();
  });

  it("deletes the date of birth only after confirmation", async () => {
    current = { ...nothing, date_of_birth: "1958-04-17", age_band: "60+" };
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByRole("button", { name: "Delete date of birth" }));
    const dialog = await screen.findByRole("dialog");
    expect(sent("/customers/me/date-of-birth", "DELETE")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Date of birth deleted.")).toBeInTheDocument();
    expect(await screen.findByText("Not provided")).toBeInTheDocument();
    expect(sent("/customers/me/date-of-birth", "DELETE")).toHaveLength(1);
  });

  it("grants and withdraws the separate consent explicitly", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByRole("button", { name: "Give consent" }));
    expect(await screen.findByRole("button", { name: "Withdraw consent" })).toBeInTheDocument();
    expect(screen.getByText(/^Yes, since/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Withdraw consent" }));
    expect(await screen.findByRole("button", { name: "Give consent" })).toBeInTheDocument();

    const bodies = sent("/customers/me/recommendations-consent", "PUT").map(
      ([, init]) => JSON.parse(init?.body as string) as unknown,
    );
    expect(bodies).toEqual([{ granted: true }, { granted: false }]);
    expect(sent("/customers/me/date-of-birth", "PUT")).toHaveLength(0); // consent is separate
  });
});
