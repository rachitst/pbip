import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, customerMe, json, renderApp, urlOf } from "../test-utils";

describe("opening an account", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let accounts = [account];
  let limitReached = false;

  beforeEach(() => {
    accounts = [account];
    limitReached = false;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/accounts") && init?.method === "POST") {
        if (limitReached) {
          return Promise.resolve(
            json(422, {
              code: "account_limit_exceeded",
              title: "customer cannot have more than 5 active accounts",
            }),
          );
        }
        const opened = {
          ...account,
          id: "acc-2",
          account_type: "CURRENT",
          account_number_masked: "••••••••0007",
        };
        accounts = [...accounts, opened];
        return Promise.resolve(json(201, opened));
      }
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, accounts));
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens a checking account and refreshes the list", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.selectOptions(await screen.findByLabelText("Account type"), "CHECKING");
    await user.click(screen.getByRole("button", { name: "Open account" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Account ••••••••0007 opened.");
    expect(await screen.findByText("••••••••0007")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([input, init]) => urlOf(input).endsWith("/accounts") && init?.method === "POST",
    );
    expect(JSON.parse(post?.[1]?.body as string) as unknown).toEqual({ account_type: "CHECKING" });
  });

  it("explains the five-account limit", async () => {
    limitReached = true;
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Open account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("more than 5 active accounts");
  });
});
