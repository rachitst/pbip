import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NotificationBell } from "./NotificationBell";

function urlOf(input: Parameters<typeof fetch>[0]): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const mockTransferPosted = {
  id: "notif-1",
  type: "TRANSFER_POSTED",
  title: "Transfer completed",
  body: "Your transfer of INR 500.00 was completed.",
  read: false,
  created_at: "2026-10-31T10:00:00Z",
};

const mockSecurityAlert = {
  id: "notif-2",
  type: "SECURITY_ALERT",
  title: "New device detected",
  body: "A new browser session was registered on your account.",
  read: false,
  created_at: "2026-10-31T09:30:00Z",
};

const mockStatement = {
  id: "notif-3",
  type: "STATEMENT_AVAILABLE",
  title: "Statement ready",
  body: "Your monthly statement for September 2026 is ready.",
  read: true,
  created_at: "2026-10-30T08:00:00Z",
};

const mockNotificationsPage = {
  items: [mockTransferPosted, mockSecurityAlert, mockStatement],
  total: 3,
  unread_count: 2,
  limit: 20,
  offset: 0,
};

function renderNotificationBell(queryClient?: QueryClient) {
  const client =
    queryClient ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0 },
        mutations: { retry: false },
      },
    });

  return render(
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route
            path="*"
            element={
              <div>
                <NotificationBell />
                <div data-testid="page-destination">Default Page</div>
              </div>
            }
          />
          <Route path="/accounts" element={<div data-testid="accounts-page">Accounts Page</div>} />
          <Route path="/profile" element={<div data-testid="profile-page">Profile Page</div>} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>,
  );
}

describe("Customer Real-Time Notifications (TASK-022)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("1. Notification Badge", () => {
    it("displays zero unread notifications without badge indicator", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(
            json(200, {
              items: [mockStatement],
              total: 1,
              unread_count: 0,
              limit: 20,
              offset: 0,
            }),
          );
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", {
        name: "Notifications (no unread)",
      });
      expect(bell).toBeInTheDocument();
      expect(screen.queryByText("0")).not.toBeInTheDocument();
    });

    it("displays a positive unread count badge and updates accessible name", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", {
        name: "Notifications (2 unread)",
      });
      expect(bell).toBeInTheDocument();
      expect(screen.getByText("2")).toBeInTheDocument();
    });

    it("caps large unread counts at 99+", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(
            json(200, {
              items: [],
              total: 150,
              unread_count: 150,
              limit: 20,
              offset: 0,
            }),
          );
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      await screen.findByRole("button", { name: "Notifications (150 unread)" });
      expect(screen.getByText("99+")).toBeInTheDocument();
    });

    it("handles notification query error without crashing", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(500, { code: "internal_error", title: "DB Error" }));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: "Notifications (no unread)" });
      expect(bell).toBeInTheDocument();
    });
  });

  describe("2. Notification Dropdown Tray", () => {
    it("opens tray on bell click and closes on close button click", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      expect(bell).toHaveAttribute("aria-expanded", "false");

      // Open tray
      await userEvent.click(bell);
      expect(bell).toHaveAttribute("aria-expanded", "true");

      const dialog = screen.getByRole("dialog", { name: "Notifications tray" });
      expect(dialog).toBeInTheDocument();
      expect(within(dialog).getByRole("heading", { name: "Notifications" })).toBeInTheDocument();

      // Close tray using close button
      const closeBtn = within(dialog).getByRole("button", { name: "Close notifications tray" });
      await userEvent.click(closeBtn);

      expect(bell).toHaveAttribute("aria-expanded", "false");
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    it("closes tray on pressing Escape key and restores focus to bell button", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      expect(screen.getByRole("dialog")).toBeInTheDocument();

      await userEvent.keyboard("{Escape}");

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(bell).toHaveFocus();
    });

    it("displays transfer notifications, security alerts, and statements with tags", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const dialog = screen.getByRole("dialog");

      // Transfer notification
      expect(within(dialog).getByText("Transfer completed")).toBeInTheDocument();
      expect(
        within(dialog).getByText("Your transfer of INR 500.00 was completed."),
      ).toBeInTheDocument();
      expect(within(dialog).getByText("Transfer")).toBeInTheDocument();

      // Security alert
      expect(within(dialog).getByText("New device detected")).toBeInTheDocument();
      expect(
        within(dialog).getByText("A new browser session was registered on your account."),
      ).toBeInTheDocument();
      expect(within(dialog).getByText("Security Alert")).toBeInTheDocument();

      // Statement
      expect(within(dialog).getByText("Statement ready")).toBeInTheDocument();
      expect(within(dialog).getByText("Statement")).toBeInTheDocument();
    });

    it("shows loading state when notifications query is pending", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return new Promise(() => {}); // never resolves
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = screen.getByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      expect(screen.getByText("Loading notifications...")).toBeInTheDocument();
    });

    it("shows empty state when user has no notifications", async () => {
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(
            json(200, {
              items: [],
              total: 0,
              unread_count: 0,
              limit: 20,
              offset: 0,
            }),
          );
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      expect(screen.getByText("No notifications yet.")).toBeInTheDocument();
    });

    it("shows error state with retry button on query failure", async () => {
      let fail = true;
      fetchMock.mockImplementation((input) => {
        const url = urlOf(input);
        if (url.includes("/api/v1/notifications")) {
          if (fail) {
            return Promise.resolve(
              json(400, { code: "inbox_error", title: "Could not fetch notifications" }),
            );
          }
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = screen.getByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const errorText = await screen.findByText("Could not fetch notifications");
      expect(errorText).toBeInTheDocument();

      fail = false;
      const retryBtn = screen.getByRole("button", { name: "Try again" });
      await userEvent.click(retryBtn);

      await waitFor(() => {
        expect(screen.getByText("Transfer completed")).toBeInTheDocument();
      });
    });
  });

  describe("3. Notification Interactions & Navigation", () => {
    it("marks an unread notification as read and navigates to /accounts on transfer click", async () => {
      let markReadCalled = false;
      let targetNotificationId = "";

      fetchMock.mockImplementation((input, init) => {
        const url = urlOf(input);
        if (url.endsWith("/read") && init?.method === "POST") {
          markReadCalled = true;
          targetNotificationId = url.split("/")[url.split("/").length - 2] ?? "";
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const transferItem = screen.getByRole("button", { name: /Transfer completed/ });
      await userEvent.click(transferItem);

      expect(markReadCalled).toBe(true);
      expect(targetNotificationId).toBe("notif-1");
      expect(await screen.findByTestId("accounts-page")).toBeInTheDocument();
    });

    it("does not call mark-as-read API when clicking an already read notification", async () => {
      let markReadCalled = false;

      fetchMock.mockImplementation((input, init) => {
        const url = urlOf(input);
        if (url.endsWith("/read") && init?.method === "POST") {
          markReadCalled = true;
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const statementItem = screen.getByRole("button", { name: /Statement ready/ });
      await userEvent.click(statementItem);

      expect(markReadCalled).toBe(false);
      expect(await screen.findByTestId("accounts-page")).toBeInTheDocument();
    });

    it("navigates to /profile when clicking a security alert notification", async () => {
      fetchMock.mockImplementation((input, init) => {
        const url = urlOf(input);
        if (url.endsWith("/read") && init?.method === "POST") {
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const securityItem = screen.getByRole("button", { name: /New device detected/ });
      await userEvent.click(securityItem);

      expect(await screen.findByTestId("profile-page")).toBeInTheDocument();
    });

    it("bulk marks all notifications as read when clicking 'Mark all as read'", async () => {
      let markAllCalled = false;

      fetchMock.mockImplementation((input, init) => {
        const url = urlOf(input);
        if (url.endsWith("/mark-all-read") && init?.method === "POST") {
          markAllCalled = true;
          return Promise.resolve(json(200, { updated_count: 2 }));
        }
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, mockNotificationsPage));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();

      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);

      const markAllBtn = screen.getByRole("button", { name: "Mark all as read" });
      await userEvent.click(markAllBtn);

      expect(markAllCalled).toBe(true);
    });
  });
});
