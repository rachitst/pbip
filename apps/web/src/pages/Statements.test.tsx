import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountOut } from "../api/client";
import { json, urlOf } from "../test-utils";
import { Statements, defaultPeriod, istDate, periodProblem } from "./Statements";

const account: AccountOut = {
  id: "a-1",
  account_number: "100000005678",
  account_number_masked: "••••••••5678",
  account_type: "SAVINGS",
  available_minor: 0,
  currency: "INR",
  held_minor: 0,
  ledger_balance_minor: 0,
  status: "ACTIVE",
};
// 2 Nov 2026 03:00 IST is still 1 Nov in UTC: the period must use India's date.
const NOW = Date.parse("2026-11-01T21:30:00Z");

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Statements now={NOW} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }),
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("statement period rules", () => {
  it("uses India's calendar day and defaults to this month so far", () => {
    expect(istDate(NOW)).toBe("2026-11-02");
    expect(defaultPeriod(NOW)).toEqual({ start: "2026-11-01", end: "2026-11-02" });
  });

  it("rejects reversed, future and over-long periods", () => {
    expect(periodProblem("2026-10-02", "2026-10-01", "2026-11-02")).toMatch(/on or before/);
    expect(periodProblem("2026-11-01", "2026-11-03", "2026-11-02")).toMatch(/future/);
    expect(periodProblem("2025-01-01", "2026-01-02", "2026-11-02")).toMatch(/366 days/);
    expect(periodProblem("2025-11-01", "2026-10-31", "2026-11-02")).toBeNull();
  });
});

describe("Statements page (TASK-074)", () => {
  it("downloads the chosen format through an authenticated blob request", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (urlOf(input) === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
      return Promise.resolve(
        new Response("date,description\r\n", {
          status: 200,
          headers: {
            "Content-Type": "text/csv",
            "Content-Disposition": 'attachment; filename="pbip-statement-5678.csv"',
          },
        }),
      );
    });
    renderPage();
    await user.click(await screen.findByLabelText(/CSV/));
    await user.click(screen.getByRole("button", { name: "Download statement" }));
    expect(await screen.findByText("Downloaded pbip-statement-5678.csv.")).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([input]) => urlOf(input).includes("/statements"));
    expect(urlOf(call?.[0] ?? "")).toBe(
      "/api/v1/accounts/a-1/statements?start_date=2026-11-01&end_date=2026-11-02&format=csv",
    );
  });

  it("checks the period before calling the API and shows server errors", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input) =>
        Promise.resolve(
          urlOf(input) === "/api/v1/accounts"
            ? json(200, [account])
            : json(422, { code: "unprocessable", title: "too many transactions in this period" }),
        ),
      );
    renderPage();
    const from = await screen.findByLabelText("From");
    fireEvent.change(from, { target: { value: "2026-10-20" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-10-10" } });
    await user.click(screen.getByRole("button", { name: "Download statement" }));
    expect(screen.getByRole("alert")).toHaveTextContent("on or before");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.change(from, { target: { value: "2026-10-01" } });
    await user.click(screen.getByRole("button", { name: "Download statement" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("too many transactions");
  });

  it("shows empty and error states", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, []));
    const { unmount } = renderPage();
    expect(await screen.findByText("You have no accounts yet.")).toBeInTheDocument();
    unmount();
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(500, { title: "x" }));
    renderPage();
    expect(await screen.findByText(/Could not load your accounts/)).toBeInTheDocument();
  });
});
