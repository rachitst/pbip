import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RiskProfileOut, RiskQuestionnaire } from "../api/client";
import { json, urlOf } from "../test-utils";
import { RiskProfileSection } from "./RiskProfileSection";

const URL_PATH = "/api/v1/customers/me/risk-profile";

const empty: RiskProfileOut = {
  band: null,
  score: null,
  max_score: 10,
  answers: null,
  questionnaire_version: null,
  assessed_at: null,
};

const answers: RiskQuestionnaire = {
  investment_horizon: "OVER_5_YEARS",
  loss_reaction: "WAIT_AND_HOLD",
  investment_goal: "STEADY_GROWTH",
  investment_experience: "SOME",
  emergency_fund: "OVER_6_MONTHS",
};

const assessed: RiskProfileOut = {
  band: "MODERATE",
  score: 6,
  max_score: 10,
  answers,
  questionnaire_version: "RTQ-1",
  assessed_at: "2026-11-02T09:30:00Z",
};

function renderSection() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <RiskProfileSection />
    </QueryClientProvider>,
  );
}

describe("risk profile questionnaire", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let current: RiskProfileOut = empty;
  let failGet = false;

  beforeEach(() => {
    current = empty;
    failGet = false;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      if (!urlOf(input).endsWith(URL_PATH)) {
        return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
      }
      if (init?.method === "PUT") {
        const sent = JSON.parse(init.body as string) as RiskQuestionnaire;
        current = { ...assessed, answers: sent };
        return Promise.resolve(json(200, current));
      }
      if (failGet) return Promise.resolve(json(500, { code: "error", title: "boom" }));
      return Promise.resolve(json(200, current));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const puts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT");

  it("shows a loading state, then an empty state before the first assessment", async () => {
    renderSection();
    expect(screen.getByRole("status")).toHaveTextContent("Loading your risk profile");
    expect(
      await screen.findByText("You have not answered the questionnaire yet."),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("group")).toHaveLength(5);
  });

  it("shows an error state with a retry", async () => {
    failGet = true;
    const user = userEvent.setup();
    renderSection();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your risk profile");
    failGet = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText("You have not answered the questionnaire yet."),
    ).toBeInTheDocument();
  });

  it("refuses to submit until every question is answered", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByLabelText("More than five years"));
    await user.click(screen.getByRole("button", { name: "Save my answers" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Answer all five questions");
    expect(puts()).toHaveLength(0);
  });

  it("sends the enumerated answers and shows the band the server computed", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByLabelText("More than five years"));
    await user.click(screen.getByLabelText("Wait and hold"));
    await user.click(screen.getByLabelText("Steady growth with some ups and downs"));
    await user.click(screen.getByLabelText("Some"));
    await user.click(screen.getByLabelText("More than 6 months"));
    await user.click(screen.getByRole("button", { name: "Save my answers" }));

    expect(await screen.findByText("Saved. Your risk band is moderate.")).toBeInTheDocument();
    expect(screen.getByText(/^Moderate: you accept some ups and downs/)).toBeInTheDocument();
    expect(screen.getByText("Last assessed")).toBeInTheDocument();
    expect(JSON.parse(puts()[0]?.[1]?.body as string)).toEqual(answers);
  });

  it("pre-selects the answers that produced the current band", async () => {
    current = assessed;
    renderSection();
    expect(await screen.findByLabelText("Wait and hold")).toBeChecked();
    expect(screen.getByLabelText("More than five years")).toBeChecked();
    expect(screen.getByLabelText("Sell everything to stop further losses")).not.toBeChecked();
  });

  it("shows a server rejection without losing the answers", async () => {
    current = assessed;
    fetchMock.mockImplementation((_input, init) =>
      Promise.resolve(
        init?.method === "PUT"
          ? json(422, { code: "validation_error", title: "Request validation failed" })
          : json(200, current),
      ),
    );
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByRole("button", { name: "Save my answers" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByLabelText("Wait and hold")).toBeChecked();
  });
});
