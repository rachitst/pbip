import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { NotificationBell, badgeText, notificationDestination } from "./NotificationBell";

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
  resource_type: "transaction" as const,
  resource_id: "txn-1",
  account_id: "acc-1",
};

// The API produces only transfer outcomes (modules/notifications/service.py).
const mockUnderReview = {
  id: "notif-2",
  type: "TRANSFER_UNDER_REVIEW",
  title: "Transfer under review",
  body: "Your transfer of INR 25,000.00 is being reviewed for your safety.",
  read: false,
  created_at: "2026-10-31T09:30:00Z",
  resource_type: "transaction" as const,
  resource_id: "txn-2",
  account_id: "acc-1",
};

const mockDeclined = {
  id: "notif-3",
  type: "TRANSFER_DECLINED",
  title: "Transfer not completed",
  body: "Your transfer of INR 90,000.00 could not be completed.",
  read: true,
  created_at: "2026-10-30T08:00:00Z",
  // sent before notifications carried link targets: opens Accounts without a highlight
  resource_type: "transaction" as const,
  resource_id: null,
  account_id: null,
};

const mockNotificationsPage = {
  items: [mockTransferPosted, mockUnderReview, mockDeclined],
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
              items: [mockDeclined],
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

    it("labels each transfer outcome the API produces", async () => {
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
      // a completed transfer has no type tag: its title already says what it is
      expect(within(dialog).queryByText("Transfer")).not.toBeInTheDocument();

      // Held for review: a transfer outcome, not a "security alert"
      expect(within(dialog).getByText("Transfer under review")).toBeInTheDocument();
      expect(within(dialog).getByText("Under review")).toBeInTheDocument();

      // Declined
      expect(within(dialog).getByText("Transfer not completed")).toBeInTheDocument();
      expect(within(dialog).getByText("Not completed")).toBeInTheDocument();
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

      const declinedItem = screen.getByRole("button", { name: /Transfer not completed/ });
      await userEvent.click(declinedItem);

      expect(markReadCalled).toBe(false);
      expect(await screen.findByTestId("accounts-page")).toBeInTheDocument();
    });

    it("marks an unknown notification type read without guessing a destination", async () => {
      const marked: string[] = [];
      const unknown = {
        ...mockTransferPosted,
        id: "notif-9",
        type: "SOMETHING_NEW",
        title: "New",
        resource_type: null,
        resource_id: null,
        account_id: null,
      };
      fetchMock.mockImplementation((input, init) => {
        const url = urlOf(input);
        if (url.endsWith("/read") && init?.method === "POST") {
          marked.push(url);
          return Promise.resolve(new Response(null, { status: 204 }));
        }
        if (url.includes("/api/v1/notifications")) {
          return Promise.resolve(json(200, { ...mockNotificationsPage, items: [unknown] }));
        }
        return Promise.resolve(json(404, { detail: "not found" }));
      });

      renderNotificationBell();
      const bell = await screen.findByRole("button", { name: /Notifications/ });
      await userEvent.click(bell);
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByText("Notification")).toBeInTheDocument(); // neutral label
      await userEvent.click(within(dialog).getByRole("button", { name: /New/ }));

      await waitFor(() => {
        expect(marked).toEqual(["/api/v1/notifications/notif-9/read"]);
      });
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.queryByTestId("accounts-page")).not.toBeInTheDocument();
      expect(screen.queryByTestId("profile-page")).not.toBeInTheDocument();
      expect(bell).toHaveFocus();
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

describe("TASK-022 review fixes", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("caps the visible count at 99+", () => {
    expect(badgeText(7)).toBe("7");
    expect(badgeText(99)).toBe("99");
    expect(badgeText(100)).toBe("99+");
  });

  it("keeps notification items valid: only phrasing content inside each button", async () => {
    fetchMock.mockResolvedValue(json(200, mockNotificationsPage));
    renderNotificationBell();
    await userEvent.click(await screen.findByRole("button", { name: /Notifications/ }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(3);
    expect(dialog.querySelectorAll(".notification-item p, .notification-item div")).toHaveLength(0);
  });

  it("styles the bell and tray with theme tokens only", () => {
    const css = readFileSync("src/styles.css", "utf8"); // vitest runs from apps/web
    const start = css.indexOf("/* ---- Notifications");
    const block = css.slice(start, css.indexOf("/* ---- ", start + 10));
    expect(block.length).toBeGreaterThan(0);
    expect(block).not.toMatch(/#[0-9a-f]{3,8}\b/i); // no hard-coded colours
    expect(block).not.toMatch(/rgba?\(/);
    expect(block).not.toMatch(/--shadow-lg/); // not a token
    expect(block).not.toMatch(/font-size:\s*[0-9.]+(rem|px)/); // sizes come from --text-*
  });
});

describe("notification link targets (#45)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("builds the destination from the link target", () => {
    expect(notificationDestination(mockTransferPosted)).toBe(
      "/accounts?account=acc-1&transaction=txn-1",
    );
    expect(notificationDestination(mockDeclined)).toBe("/accounts");
    expect(
      notificationDestination({
        ...mockTransferPosted,
        type: "PASSWORD_CHANGED",
        resource_type: "security",
        resource_id: null,
        account_id: null,
      }),
    ).toBe("/profile");
    expect(
      notificationDestination({ ...mockTransferPosted, type: "NEW", resource_type: null }),
    ).toBeNull();
  });

  it("opens Profile & security for a security notification", async () => {
    const security = {
      ...mockTransferPosted,
      id: "notif-7",
      type: "DEVICE_REGISTERED",
      title: "New device registered",
      body: "A new device was registered on your account.",
      resource_type: "security" as const,
      resource_id: null,
      account_id: null,
    };
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      if (url.endsWith("/read") && init?.method === "POST") {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(json(200, { ...mockNotificationsPage, items: [security] }));
    });
    renderNotificationBell();
    await userEvent.click(await screen.findByRole("button", { name: /Notifications/ }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Security")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: /New device registered/ }));
    expect(await screen.findByTestId("profile-page")).toBeInTheDocument();
  });
});
