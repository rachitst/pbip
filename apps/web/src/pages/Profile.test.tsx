import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, json, noContent, renderApp, urlOf } from "../test-utils";

const profile = {
  id: "cust-1",
  user_id: "user-1",
  email: "alice@demo.pbip",
  full_name: "Alice Demo",
  date_of_birth: null,
  phone_masked: "******4321",
  kyc_status: "VERIFIED",
  created_at: "2026-01-15T00:00:00Z",
};
const device = {
  id: "dev-1",
  label: "Firefox on Linux",
  first_seen_at: "2026-10-01T00:00:00Z",
  last_seen_at: "2026-10-20T00:00:00Z",
  trusted: false,
  device_age_days: 24,
};

type ChangeOutcome = "ok" | "wrong-current" | "breached";

describe("profile & security", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let changeOutcome: ChangeOutcome = "ok";

  beforeEach(() => {
    changeOutcome = "ok";
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/logout")) return Promise.resolve(noContent());
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/customers/me")) return Promise.resolve(json(200, profile));
      if (url.endsWith("/customers/devices") && init?.method === "POST") {
        return Promise.resolve(json(201, { ...device, id: "dev-2", label: "This browser" }));
      }
      if (url.endsWith("/customers/devices")) return Promise.resolve(json(200, [device]));
      if (url.endsWith("/auth/password-change")) {
        if (changeOutcome === "wrong-current") {
          return Promise.resolve(
            json(403, { code: "invalid_current_password", title: "incorrect current password" }),
          );
        }
        if (changeOutcome === "breached") {
          return Promise.resolve(
            json(422, {
              code: "unprocessable",
              title: "password is too common or has been breached",
            }),
          );
        }
        return Promise.resolve(noContent());
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function openProfile() {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Profile & security" }));
    return user;
  }

  async function fillPassword(
    user: ReturnType<typeof userEvent.setup>,
    next: string,
    confirm = next,
  ) {
    await user.type(screen.getByLabelText("Current password"), "Old-Password-123!");
    await user.type(screen.getByLabelText("New password"), next);
    await user.type(screen.getByLabelText("Confirm new password"), confirm);
    await user.click(screen.getByRole("button", { name: "Change password" }));
  }

  function calls(path: string) {
    return fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith(path));
  }

  it("shows the profile with masked phone and the registered devices", async () => {
    await openProfile();
    expect(await screen.findAllByText("Alice Demo")).not.toHaveLength(0);
    expect(screen.getByText("******4321")).toBeInTheDocument();
    expect(await screen.findByText("Firefox on Linux")).toBeInTheDocument();
    expect(screen.getByText("New (24 days)")).toBeInTheDocument();
  });

  it("registers this browser with a stable X-Device-ID header", async () => {
    const user = await openProfile();
    await user.click(await screen.findByRole("button", { name: "Register this browser" }));
    expect(await screen.findByText("This browser is registered.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Register this browser" }));

    const posts = calls("/customers/devices").filter(([, init]) => init?.method === "POST");
    const ids = posts.map(([, init]) => new Headers(init?.headers).get("X-Device-ID"));
    expect(ids).toHaveLength(2);
    expect(ids[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(ids[1]).toBe(ids[0]);
  });

  it("validates the new password locally before calling the API", async () => {
    const user = await openProfile();
    await screen.findAllByText("Alice Demo");
    await fillPassword(user, "New-Password-456!", "Different-456!!");
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    expect(calls("/auth/password-change")).toHaveLength(0);
  });

  it("signs out everywhere after a successful change and says why", async () => {
    const user = await openProfile();
    await screen.findAllByText("Alice Demo");
    await fillPassword(user, "New-Password-456!");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("All your sessions were signed out");
    const body = JSON.parse(calls("/auth/password-change")[0]?.[1]?.body as string) as unknown;
    expect(body).toEqual({
      current_password: "Old-Password-123!", // pragma: allowlist secret
      new_password: "New-Password-456!", // pragma: allowlist secret
    });
  });

  it("reports a wrong current password without retrying or losing the session", async () => {
    changeOutcome = "wrong-current";
    const user = await openProfile();
    await screen.findAllByText("Alice Demo");
    const refreshesBefore = calls("/auth/refresh").length;
    await fillPassword(user, "New-Password-456!");

    expect(await screen.findByRole("alert")).toHaveTextContent("current password is incorrect");
    expect(calls("/auth/password-change")).toHaveLength(1); // 403 is not replayed like a 401
    expect(calls("/auth/refresh")).toHaveLength(refreshesBefore);
    expect(screen.getByRole("heading", { name: "Profile & security" })).toBeInTheDocument();
  });

  it("shows the server's password-policy message", async () => {
    changeOutcome = "breached";
    const user = await openProfile();
    await screen.findAllByText("Alice Demo");
    await fillPassword(user, "Summer2025!!!!");
    expect(await screen.findByRole("alert")).toHaveTextContent("too common or has been breached");
  });
});
