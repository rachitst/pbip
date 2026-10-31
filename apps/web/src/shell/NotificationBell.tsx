import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import type { NotificationOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDateTime } from "../lib/format";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

type Tone = "success" | "warning" | "danger" | "neutral";

/** The notification types the API produces (modules/notifications/service.py: transfer outcomes
 * only). A notification carries no transaction id and there is no transaction detail page, so a
 * transfer notification opens Accounts, where each account's transactions are listed. */
const NOTIFICATION_TYPES: Record<string, { label: string; tone: Tone; to: string }> = {
  TRANSFER_POSTED: { label: "Transfer", tone: "success", to: "/accounts" },
  TRANSFER_UNDER_REVIEW: { label: "Under review", tone: "warning", to: "/accounts" },
  TRANSFER_DECLINED: { label: "Not completed", tone: "danger", to: "/accounts" },
};

/** A type the API adds later is shown neutrally and only marked read: no destination is guessed. */
export function notificationKind(type: string): { label: string; tone: Tone; to: string | null } {
  return NOTIFICATION_TYPES[type] ?? { label: "Notification", tone: "neutral", to: null };
}

export function badgeText(unread: number): string {
  return unread > 99 ? "99+" : String(unread);
}

export function NotificationBell() {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const bellId = useId();
  const trayId = useId();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // Polls every 15 s while the tab is visible (TanStack pauses intervals in background tabs). A
  // 401 that cannot be refreshed signs the user out, which unmounts the bell and stops polling.
  const notificationsQuery = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.notifications({ limit: 20 }),
    refetchInterval: 15_000,
  });

  const markReadMutation = useMutation({
    mutationFn: (id: string) => api.markNotificationRead(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  const markAllReadMutation = useMutation({
    mutationFn: () => api.markAllNotificationsRead(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  const unreadCount = notificationsQuery.data?.unread_count ?? 0;
  const items = notificationsQuery.data?.items ?? [];

  const close = (returnFocus: boolean) => {
    setIsOpen(false);
    if (returnFocus) document.getElementById(bellId)?.focus();
  };

  useEffect(() => {
    if (!isOpen) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setIsOpen(false);
        document.getElementById(bellId)?.focus();
      }
    }

    function handleClickOutside(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleClickOutside);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [isOpen, bellId]);

  const handleNotificationClick = (item: NotificationOut) => {
    if (!item.read) {
      markReadMutation.mutate(item.id);
    }
    const destination = notificationKind(item.type).to;
    // Navigating moves focus to the new page's heading; otherwise focus returns to the bell.
    close(destination === null);
    if (destination !== null) void navigate(destination);
  };

  const accessibleBellLabel =
    unreadCount > 0 ? `Notifications (${unreadCount} unread)` : "Notifications (no unread)";

  return (
    <div ref={containerRef} className="notification-bell">
      <Button
        id={bellId}
        variant="ghost"
        size="sm"
        className="notification-bell__button"
        aria-label={accessibleBellLabel}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-controls={isOpen ? trayId : undefined}
        onClick={() => {
          setIsOpen((prev) => !prev);
        }}
      >
        <Icon name="bell" size={16} />
        {unreadCount > 0 && (
          <span className="notification-bell__count" aria-hidden="true">
            {badgeText(unreadCount)}
          </span>
        )}
      </Button>

      {isOpen && (
        <div
          id={trayId}
          role="dialog"
          aria-label="Notifications tray"
          aria-modal="false"
          className="notification-tray"
        >
          <div className="notification-tray__header">
            <div className="notification-tray__title-group">
              <h2 className="notification-tray__title">Notifications</h2>
              {unreadCount > 0 && (
                <span className="badge badge--neutral">{unreadCount} unread</span>
              )}
            </div>
            <div className="notification-tray__actions">
              {unreadCount > 0 && (
                <Button
                  variant="link"
                  size="sm"
                  loading={markAllReadMutation.isPending}
                  onClick={() => {
                    markAllReadMutation.mutate();
                  }}
                >
                  Mark all as read
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                aria-label="Close notifications tray"
                onClick={() => {
                  close(true);
                }}
              >
                <Icon name="close" size={14} />
              </Button>
            </div>
          </div>

          <div className="notification-tray__content">
            {notificationsQuery.isPending && <LoadingState label="Loading notifications..." />}

            {notificationsQuery.isError && (
              <ErrorState
                message={errorMessage(notificationsQuery.error, "Could not load notifications.")}
                onRetry={() => {
                  void notificationsQuery.refetch();
                }}
              />
            )}

            {notificationsQuery.isSuccess && items.length === 0 && (
              <EmptyState message="No notifications yet." />
            )}

            {notificationsQuery.isSuccess && items.length > 0 && (
              <ul className="notification-list">
                {items.map((item) => {
                  const kind = notificationKind(item.type);
                  return (
                    <li key={item.id}>
                      <button
                        type="button"
                        className={`notification-item${item.read ? "" : " notification-item--unread"}`}
                        onClick={() => {
                          handleNotificationClick(item);
                        }}
                      >
                        <span className="notification-item__top">
                          <span className={`badge badge--${kind.tone}`}>{kind.label}</span>
                          <time className="muted" dateTime={item.created_at}>
                            {formatDateTime(item.created_at)}
                          </time>
                        </span>
                        <span className="notification-item__heading">
                          {!item.read && (
                            <span className="notification-item__dot" aria-hidden="true">
                              ●
                            </span>
                          )}
                          <strong className="notification-item__title">
                            {!item.read && <span className="sr-only">(Unread) </span>}
                            {item.title}
                          </strong>
                        </span>
                        <span className="notification-item__body muted">{item.body}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
