import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, customerMe, entry, json, renderApp, urlOf } from "../test-utils";

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
      if (url.pathname === "/api/v1/payees") return Promise.resolve(json(200, []));
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
      return Promise.resolve(json(200, { items: [], next_cursor: null, has_more: false }));
    });
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Transactions" }));
    expect(await screen.findByText("No transactions yet.")).toBeInTheDocument();
  });
});
