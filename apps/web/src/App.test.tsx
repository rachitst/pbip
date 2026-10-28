import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, json, profile, renderApp, urlOf } from "./test-utils";

describe("App shell, session bar, and theme toggle (TASK-019)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);

    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/customers/me")) return Promise.resolve(json(200, profile));
      if (url.endsWith("/customers/devices")) return Promise.resolve(json(200, []));
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders navigation shell with customer session bar details", async () => {
    renderApp();

    // Verify session bar rendered with authenticated customer details
    // Wait for profile to load before checking session bar content
    expect(await screen.findByText(/\(\s*alice@demo\.pbip\s*\)/)).toBeInTheDocument();
    expect(screen.getByLabelText("session status")).toHaveTextContent("Active session");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();

    // Verify responsive navigation tabs
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accounts" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "Profile & security" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("toggles between dark and light themes and persists in localStorage", async () => {
    const user = userEvent.setup();
    renderApp();

    const themeBtn = await screen.findByRole("button", { name: /switch to (dark|light) theme/i });
    expect(themeBtn).toBeInTheDocument();

    // Toggle theme
    await user.click(themeBtn);
    const newTheme = document.documentElement.getAttribute("data-theme");
    expect(newTheme).toMatch(/^(dark|light)$/);
    expect(window.localStorage.getItem("pbip-theme")).toBe(newTheme);

    // Toggle back
    await user.click(themeBtn);
    const toggledAgain = document.documentElement.getAttribute("data-theme");
    expect(toggledAgain).not.toBe(newTheme);
    expect(window.localStorage.getItem("pbip-theme")).toBe(toggledAgain);
  });

  it("applies a saved theme on the sign-in page before authentication", async () => {
    window.localStorage.setItem("pbip-theme", "dark");
    fetchMock.mockImplementation(() =>
      Promise.resolve(json(401, { code: "unauthorized", title: "no session" })),
    );
    renderApp();

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("follows the OS preference without saving it until the user toggles", async () => {
    renderApp();

    const themeBtn = await screen.findByRole("button", { name: /switch to (dark|light) theme/i });
    expect(document.documentElement.getAttribute("data-theme")).toBe("light"); // jsdom default
    expect(window.localStorage.getItem("pbip-theme")).toBeNull();

    await userEvent.setup().click(themeBtn);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem("pbip-theme")).toBe("dark");
  });

  it("switches pages between Accounts and Profile cleanly", async () => {
    const user = userEvent.setup();
    renderApp();

    // Accounts is open by default
    expect(await screen.findByRole("heading", { name: "My accounts" })).toBeInTheDocument();

    // Switch to Profile
    await user.click(screen.getByRole("button", { name: "Profile & security" }));
    expect(await screen.findByRole("heading", { name: "Profile & security" })).toBeInTheDocument();

    // Switch back to Accounts
    await user.click(screen.getByRole("button", { name: "Accounts" }));
    expect(await screen.findByRole("heading", { name: "My accounts" })).toBeInTheDocument();
  });

  it("handles profile loading and error in session bar without crashing", async () => {
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/accounts")) return Promise.resolve(json(200, [account]));
      if (url.endsWith("/payees")) return Promise.resolve(json(200, []));
      if (url.endsWith("/customers/me"))
        return Promise.resolve(json(500, { code: "error", title: "failed" }));
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });

    renderApp();

    // Fallback in session bar is rendered without crashing the shell
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "My accounts" })).toBeInTheDocument();
  });
});
