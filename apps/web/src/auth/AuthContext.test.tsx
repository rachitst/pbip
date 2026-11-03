import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, customerMe, json, noContent, renderApp, urlOf } from "../test-utils";

const profileOf = (name: string) => ({
  id: `cust-${name}`,
  user_id: `user-${name}`,
  email: `${name.toLowerCase()}@demo.pbip`,
  full_name: name,
  date_of_birth: null,
  phone_masked: null,
  kyc_status: "VERIFIED",
  created_at: "2026-01-01T00:00:00Z",
});

describe("signing out", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let signedIn: "Alice" | "Bob" = "Alice";

  beforeEach(() => {
    signedIn = "Alice";
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "a" }));
      if (url.endsWith("/auth/me")) return Promise.resolve(json(200, customerMe));
      if (url.endsWith("/auth/logout")) return Promise.resolve(noContent());
      if (url.endsWith("/auth/login")) return Promise.resolve(json(200, { access_token: "b" }));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/customers/devices")) return Promise.resolve(json(200, []));
      if (url.endsWith("/customers/me")) {
        // Bob's profile request never completes, so anything on screen must come from cache.
        if (signedIn === "Bob") return new Promise<Response>(() => undefined);
        return Promise.resolve(json(200, profileOf("Alice")));
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("drops the previous customer's cached data before the next person signs in", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("link", { name: "Profile & security" }));
    expect(await screen.findAllByText("Alice")).not.toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    signedIn = "Bob";
    await user.type(await screen.findByLabelText("Email"), "bob@demo.pbip");
    await user.type(screen.getByLabelText("Password"), "Bob-Password-123!"); // pragma: allowlist secret
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.click(await screen.findByRole("link", { name: "Profile & security" }));

    expect(await screen.findByText("Loading profile...")).toBeInTheDocument();
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });
});

describe("restoring a session on load", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("treats a 204 refresh (no session cookie) as signed out, with one request and no error", async () => {
    // #382: the API answers 204 instead of 401 when there is no refresh cookie, so the browser
    // has no failed request to log on every visit to the sign-in page.
    fetchMock.mockImplementation((input) =>
      Promise.resolve(
        urlOf(input).endsWith("/auth/refresh")
          ? noContent()
          : json(404, { code: "not_found", title: "not found" }),
      ),
    );
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderApp();

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => urlOf(input))).toEqual(["/api/v1/auth/refresh"]);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});
