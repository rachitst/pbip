import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { account, customerMe, json, noContent, profile, renderApp, urlOf } from "../test-utils";

const analystMe = {
  ...customerMe,
  id: "user-9",
  email: "analyst@bank.example",
  roles: ["FRAUD_ANALYST"],
  mfa_enabled: true,
  mfa_verified_session: true,
};

describe("routing, roles and the application shell (ADR-0016)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let me: unknown = customerMe;
  let meStatus = 200;

  beforeEach(() => {
    me = customerMe;
    meStatus = 200;
    document.title = "";
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/auth/refresh")) return Promise.resolve(json(200, { access_token: "t" }));
      if (url.endsWith("/auth/login")) return Promise.resolve(json(200, { access_token: "t2" }));
      if (url.endsWith("/auth/logout")) return Promise.resolve(noContent());
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(
          meStatus === 200 ? json(200, me) : json(meStatus, { code: "error", title: "boom" }),
        );
      }
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

  const requested = (suffix: string) =>
    fetchMock.mock.calls.filter(([input]) => urlOf(input).endsWith(suffix)).length;

  describe("customer routes", () => {
    it("navigates by URL, updates the title and moves focus to the new heading", async () => {
      const user = userEvent.setup();
      renderApp();
      expect(await screen.findByRole("heading", { name: "My accounts" })).toBeInTheDocument();
      expect(document.title).toBe("My accounts - PBIP");

      await user.click(screen.getByRole("link", { name: "Profile & security" }));
      const heading = await screen.findByRole("heading", { name: "Profile & security" });
      expect(window.location.pathname).toBe("/profile");
      expect(document.title).toBe("Profile & security - PBIP");
      await waitFor(() => {
        expect(heading).toHaveFocus();
      });
      expect(screen.getByRole("link", { name: "Profile & security" })).toHaveAttribute(
        "aria-current",
        "page",
      );
    });

    it("opens a deep link directly", async () => {
      window.history.replaceState(null, "", "/profile");
      renderApp();
      expect(
        await screen.findByRole("heading", { name: "Profile & security" }),
      ).toBeInTheDocument();
    });

    it("shows not found for an unknown path, inside the customer shell", async () => {
      window.history.replaceState(null, "", "/no-such-page");
      renderApp();
      expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Go to your home page" })).toHaveAttribute(
        "href",
        "/accounts",
      );
      expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    });

    it("treats a staff URL as not found for a customer and never shows staff navigation", async () => {
      window.history.replaceState(null, "", "/staff");
      renderApp();
      expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Staff workspace" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Staff workspace" })).not.toBeInTheDocument();
    });
  });

  describe("staff routes", () => {
    it("sends a staff-only user to the staff area without calling customer endpoints", async () => {
      me = analystMe;
      renderApp();
      expect(await screen.findByRole("heading", { name: "Staff workspace" })).toBeInTheDocument();
      expect(window.location.pathname).toBe("/staff");
      expect(screen.getByText("Fraud analyst")).toBeInTheDocument();
      expect(screen.getByText("analyst@bank.example", { selector: "dd" })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(requested("/accounts")).toBe(0);
      expect(requested("/customers/me")).toBe(0);
    });

    it("warns a staff user whose session did not pass MFA", async () => {
      me = { ...analystMe, mfa_verified_session: false };
      renderApp();
      const warning = await screen.findByText(/Staff tools need a session verified/);
      expect(warning).toHaveAttribute("role", "status");
      expect(warning).toHaveTextContent("Sign out and sign in again");
    });

    it("tells a staff user without an authenticator to ask for enrolment", async () => {
      me = { ...analystMe, mfa_enabled: false, mfa_verified_session: false };
      renderApp();
      expect(await screen.findByText(/Staff tools need a session verified/)).toHaveTextContent(
        "ask an administrator",
      );
    });

    it("links both areas for a user who is a customer and staff", async () => {
      me = { ...customerMe, roles: ["CUSTOMER", "COMPLIANCE_OFFICER"] };
      const user = userEvent.setup();
      renderApp();
      await user.click(await screen.findByRole("link", { name: "Staff workspace" }));
      expect(await screen.findByRole("heading", { name: "Staff workspace" })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "My banking" })).toHaveAttribute("href", "/accounts");
    });
  });

  describe("session handling", () => {
    it("offers retry and sign-out when the session cannot be loaded", async () => {
      meStatus = 500;
      const user = userEvent.setup();
      renderApp();
      expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your session.");
      meStatus = 200;
      await user.click(screen.getByRole("button", { name: "Try again" }));
      expect(await screen.findByRole("heading", { name: "My accounts" })).toBeInTheDocument();
    });

    it("re-reads roles for the next person after sign-out", async () => {
      me = analystMe;
      const user = userEvent.setup();
      renderApp();
      await screen.findByRole("heading", { name: "Staff workspace" });
      await user.click(screen.getByRole("button", { name: "Sign out" }));

      me = customerMe;
      await user.type(await screen.findByLabelText("Email"), "alice@demo.pbip");
      await user.type(screen.getByLabelText("Password"), "Alice-Password-1!"); // pragma: allowlist secret
      await user.click(screen.getByRole("button", { name: "Sign in" }));
      // The analyst's cached roles are gone: the customer is not left in the staff area.
      expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
      expect(screen.queryByText("Fraud analyst")).not.toBeInTheDocument();
      expect(requested("/auth/me")).toBe(2);
    });
  });

  describe("responsive navigation and accessibility", () => {
    it("opens and closes the menu with its button, on navigation and on Esc", async () => {
      const user = userEvent.setup();
      renderApp();
      await screen.findByRole("heading", { name: "My accounts" });
      const menu = screen.getByRole("button", { name: "Menu" });
      const nav = screen.getByRole("navigation", { name: "Main" });
      expect(menu).toHaveAttribute("aria-expanded", "false");
      expect(menu).toHaveAttribute("aria-controls", nav.id);

      await user.click(menu);
      expect(menu).toHaveAttribute("aria-expanded", "true");
      expect(nav).toHaveAttribute("data-open", "true");
      await user.click(within(nav).getByRole("link", { name: "Profile & security" }));
      await screen.findByRole("heading", { name: "Profile & security" });
      expect(menu).toHaveAttribute("aria-expanded", "false");

      await user.click(menu);
      await user.keyboard("{Escape}");
      expect(menu).toHaveAttribute("aria-expanded", "false");
    });

    it("has a skip link, landmarks and one page heading", async () => {
      renderApp();
      await screen.findByRole("heading", { name: "My accounts" });
      const skip = screen.getByRole("link", { name: "Skip to main content" });
      const main = screen.getByRole("main");
      expect(skip).toHaveAttribute("href", `#${main.id}`);
      expect(screen.getByRole("banner")).toBeInTheDocument();
      expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
      expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
      expect(screen.getAllByRole("main")).toHaveLength(1);
    });
  });
});
