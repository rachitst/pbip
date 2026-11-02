import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  account,
  customerMe,
  entry,
  json,
  noRecommendations,
  renderApp,
  urlOf,
} from "../test-utils";

describe("account transactions (keyset pagination)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let olderFails = false;

  beforeEach(() => {
    olderFails = false;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input) => {
      const url = new URL(urlOf(input), "http://localhost");
      if (url.pathname.endsWith("/auth/refresh"))
        return Promise.resolve(json(200, { access_token: "t" }));
      if (url.pathname.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.pathname === "/api/v1/accounts") return Promise.resolve(json(200, [account]));
      if (url.pathname === "/api/v1/accounts/summary")
        return Promise.resolve(json(200, { totals: [] }));
      if (url.pathname === "/api/v1/payees") return Promise.resolve(json(200, []));
      if (url.pathname === "/api/v1/recommendations")
        return Promise.resolve(json(200, noRecommendations));
      if (url.pathname === "/api/v1/accounts/acc-1/transactions") {
        if (url.searchParams.get("cursor") === "cursor-2") {
          if (olderFails) return Promise.resolve(json(500, { code: "error", title: "boom" }));
          return Promise.resolve(
            json(200, { items: [entry(1, 300)], next_cursor: null, has_more: false }),
          );
        }
        return Promise.resolve(
          json(200, {
            items: [entry(3, 500), entry(2, 400, "DEBIT")],
            next_cursor: "cursor-2",
            has_more: true,
          }),
        );
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows newest entries first and loads older pages with the server cursor", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Ref 3")).toBeInTheDocument();
    expect(within(table).getByText("-INR 4.00")).toBeInTheDocument(); // debit shown negative

    await user.click(screen.getByRole("button", { name: "Load older" }));
    expect(await within(table).findByText("Ref 1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older" })).not.toBeInTheDocument();
    const cursors = fetchMock.mock.calls
      .map(([input]) => new URL(urlOf(input), "http://localhost"))
      .filter((u) => u.pathname.endsWith("/transactions"))
      .map((u) => u.searchParams.get("cursor"));
    expect(cursors).toEqual([null, "cursor-2"]);
  });

  it("keeps loaded rows when an older page fails", async () => {
    olderFails = true;
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    await user.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await screen.findByText("Could not load older transactions.")).toBeInTheDocument();
    expect(screen.getByText("Ref 3")).toBeInTheDocument();
  });

  it("shows an empty state for an account without entries", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/accounts/summary")) return Promise.resolve(json(200, { totals: [] }));
      return Promise.resolve(json(200, { items: [], next_cursor: null, has_more: false }));
    });
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    expect(await screen.findByText("No transactions yet.")).toBeInTheDocument();
  });

  it("opens the linked account and marks the transaction a notification pointed to", async () => {
    window.history.replaceState(null, "", "/accounts?account=acc-1&transaction=t-2");
    renderApp();
    // no click needed: the account named in the link is opened
    const table = await screen.findByRole("table", { name: /Transactions, newest first/ });
    const marked = within(table)
      .getAllByRole("row")
      .filter((row) => row.getAttribute("aria-current") === "true");
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveClass("entry--highlight");
    expect(screen.getByRole("button", { name: "Hide transactions" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("sends the filters in paise and keeps them on the next page's cursor (TASK-017)", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    await screen.findByRole("table");
    await user.type(screen.getByLabelText("Search references"), "rent");
    await user.selectOptions(screen.getByLabelText("Money in or out"), "DEBIT");
    await user.type(screen.getByLabelText("Minimum amount (INR)"), "1500.50");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await user.click(await screen.findByRole("button", { name: "Load older" }));
    await screen.findByText("Ref 1");
    const calls = fetchMock.mock.calls
      .map(([input]) => new URL(urlOf(input), "http://localhost"))
      .filter((u) => u.pathname.endsWith("/transactions") && u.searchParams.has("search"));
    expect(calls.map((u) => u.searchParams.get("cursor"))).toEqual([null, "cursor-2"]);
    for (const u of calls) {
      expect(u.searchParams.get("search")).toBe("rent");
      expect(u.searchParams.get("type")).toBe("DEBIT");
      expect(u.searchParams.get("min_amount")).toBe("150050");
    }
  });

  it("checks the filters locally and clears them", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    await screen.findByRole("table");
    await user.type(screen.getByLabelText("Minimum amount (INR)"), "500");
    await user.type(screen.getByLabelText("Maximum amount (INR)"), "100");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(screen.getByRole("alert")).toHaveTextContent("must not be more than the maximum");
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Minimum amount (INR)")).toHaveValue("");
  });
});
