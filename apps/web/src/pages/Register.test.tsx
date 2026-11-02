import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, urlOf } from "../test-utils";

// Generated per run: no credential-looking literal in the repository.
const CHOSEN_PHRASE = `pw-${crypto.randomUUID()}`;

describe("registration (#201)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    // Silent session restore on page load: no cookie -> 401.
    fetchMock.mockResolvedValueOnce(json(401, { code: "unauthorized", title: "no session" }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function openForm() {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Create an account" }));
    expect(screen.getByRole("heading", { name: "Create your account" })).toBeInTheDocument();
    return user;
  }

  async function fill(user: ReturnType<typeof userEvent.setup>, password: string, confirm: string) {
    await user.type(screen.getByLabelText("Full name"), "Asha Rao");
    await user.type(screen.getByLabelText("Email"), "asha@example.com");
    await user.type(screen.getByLabelText(/^Password/), password);
    await user.type(screen.getByLabelText("Confirm password"), confirm);
    await user.click(screen.getByRole("button", { name: "Create account" }));
  }

  it("is reachable from sign-in and back", async () => {
    const user = await openForm();
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("catches mismatched passwords before calling the API", async () => {
    const user = await openForm();
    await fill(user, CHOSEN_PHRASE, `${CHOSEN_PHRASE}x`);
    expect(await screen.findByRole("alert")).toHaveTextContent("The passwords do not match.");
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the session restore
  });

  it("registers, then signs in with the same details", async () => {
    const user = await openForm();
    fetchMock
      .mockResolvedValueOnce(json(201, { id: "u-1", email: "asha@example.com", roles: [] }))
      .mockResolvedValueOnce(json(200, { access_token: "tok", expires_in: 900 }))
      .mockResolvedValue(json(401, { code: "unauthorized", title: "stop here" }));
    await fill(user, CHOSEN_PHRASE, CHOSEN_PHRASE);
    await vi.waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3);
    });
    const [register, login] = fetchMock.mock.calls.slice(1, 3);
    expect(urlOf(register?.[0] ?? "")).toBe("/api/v1/auth/register");
    expect(JSON.parse(register?.[1]?.body as string)).toEqual({
      full_name: "Asha Rao",
      email: "asha@example.com",
      password: CHOSEN_PHRASE,
    });
    expect(urlOf(login?.[0] ?? "")).toBe("/api/v1/auth/login");
  });

  it("shows the server's policy message, and a plain message when registration is off", async () => {
    const user = await openForm();
    fetchMock.mockResolvedValueOnce(
      json(422, { code: "weak_password", title: "this password appears in a breach list" }),
    );
    await fill(user, CHOSEN_PHRASE, CHOSEN_PHRASE);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this password appears in a breach list",
    );
    fetchMock.mockResolvedValueOnce(json(403, { code: "forbidden", title: "disabled" }));
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Online registration is not available.",
    );
  });

  it("says when registration is rate limited", async () => {
    const user = await openForm();
    fetchMock.mockResolvedValueOnce(json(429, { code: "rate_limited", title: "slow down" }));
    await fill(user, CHOSEN_PHRASE, CHOSEN_PHRASE);
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many requests.");
  });
});
