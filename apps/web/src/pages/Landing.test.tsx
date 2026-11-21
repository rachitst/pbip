import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, urlOf } from "../test-utils";

describe("landing page for signed-out visitors", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    // No session cookie: the silent restore on load is refused, so the visitor is signed out.
    fetchMock.mockResolvedValue(json(401, { code: "unauthorized", title: "no session" }));
  });
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    vi.unstubAllGlobals();
  });

  it("shows the landing page at / with the working sign-in form in its hero", async () => {
    renderApp();
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Everyday banking, with fraud checks that explain themselves.",
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(document.title).toBe("Finora - Smart personal banking");

    // The real form, with the names the E2E helpers and the demo recorder use.
    const form = screen.getByRole("region", { name: "Sign in" });
    expect(within(form).getByRole("heading", { level: 2, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Sign in" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Create an account" })).toBeInTheDocument();

    // The hero's "Sign in" is a link to the form, so it never competes with the submit button.
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "#sign-in");
    expect(screen.getByRole("heading", { name: "How we keep you safe" })).toBeInTheDocument();
    expect(
      screen.getAllByText(/simulated bank built for a student capstone project/i)[0],
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "Source code on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/rachitst/pbip",
    );
    expect(screen.getByRole("link", { name: "Project documentation" })).toHaveAttribute(
      "href",
      "https://github.com/rachitst/pbip/tree/main/docs",
    );
    for (const image of screen.getAllByRole("img")) {
      expect(image).toHaveAccessibleName(/demo data/);
    }
  });

  it("signs in from the hero form, including the authenticator code step", async () => {
    const user = userEvent.setup();
    renderApp();
    fetchMock.mockResolvedValueOnce(json(200, { mfa_required: true, mfa_token: "challenge" }));
    await user.type(await screen.findByLabelText("Email"), "alice@demo.pbip");
    await user.type(screen.getByLabelText("Password"), "Correct-Horse-9!"); // pragma: allowlist secret
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByLabelText("Authenticator or recovery code")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Everyday banking");
    const login = fetchMock.mock.calls.find(([input]) => urlOf(input).endsWith("/auth/login"));
    expect(login).toBeDefined();
  });

  it("switches the hero to registration with Open an account, and back with Sign in", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: "Open an account" }));

    expect(screen.getByRole("heading", { name: "Create your account" })).toBeInTheDocument();
    expect(screen.getByLabelText("Full name")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    await user.click(screen.getByRole("link", { name: "Sign in" }));
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Full name")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("keeps the plain sign-in page on a deep link, so the link survives sign-in", async () => {
    window.history.replaceState(null, "", "/payees");
    renderApp();

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open an account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "How we keep you safe" })).not.toBeInTheDocument();
    expect(document.title).toBe("Sign in - Finora");
    expect(window.location.pathname).toBe("/payees");
  });
});
