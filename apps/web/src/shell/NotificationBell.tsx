import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import type { NotificationOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDateTime } from "../lib/format";
import { Icon } from "../ui/Icon";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";

function formatNotificationType(type: string): {
  label: string;
  tone: "success" | "warning" | "danger" | "info" | "neutral";
} {
  if (type === "TRANSFER_POSTED") {
    return { label: "Transfer", tone: "success" };
  }
  if (type === "TRANSFER_UNDER_REVIEW") {
    return { label: "Security Alert", tone: "warning" };
  }
  if (type === "TRANSFER_DECLINED") {
    return { label: "Transfer Declined", tone: "danger" };
  }
  if (type.startsWith("STATEMENT")) {
    return { label: "Statement", tone: "info" };
  }
  if (type.includes("SECURITY") || type.includes("ALERT") || type.includes("LOGIN")) {
    return { label: "Security Alert", tone: "warning" };
  }
  return { label: type.replace(/_/g, " ").toLowerCase(), tone: "neutral" };
}

function resolveNotificationRoute(type: string): string {
  if (type.startsWith("TRANSFER_")) {
    return "/accounts";
  }
  if (
    type.includes("SECURITY") ||
    type.includes("ALERT") ||
    type.includes("LOGIN") ||
    type.includes("MFA") ||
    type.includes("PASSWORD")
  ) {
    return "/profile";
  }
  if (type.startsWith("STATEMENT")) {
    return "/accounts";
  }
  return "/accounts";
}

export function NotificationBell() {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

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

  useEffect(() => {
    if (!isOpen) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setIsOpen(false);
        buttonRef.current?.focus();
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
  }, [isOpen]);

  const handleNotificationClick = (item: NotificationOut) => {
    if (!item.read) {
      markReadMutation.mutate(item.id);
    }
    setIsOpen(false);
    buttonRef.current?.focus();
    const destination = resolveNotificationRoute(item.type);
    void navigate(destination);
  };

  const handleToggle = () => {
    setIsOpen((prev) => !prev);
  };

  const accessibleBellLabel =
    unreadCount > 0 ? `Notifications (${unreadCount} unread)` : "Notifications (no unread)";

  return (
    <div ref={containerRef} className="notification-bell-container">
      <button
        ref={buttonRef}
        type="button"
        className="btn btn--ghost btn--sm notification-bell-button"
        aria-label={accessibleBellLabel}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        onClick={handleToggle}
      >
        <Icon name="bell" size={16} />
        {unreadCount > 0 && (
          <span className="notification-badge" aria-hidden="true">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <div
          role="dialog"
          aria-label="Notifications tray"
          aria-modal="false"
          className="notification-tray"
        >
          <div className="notification-tray__header">
            <div className="notification-tray__title-group">
              <h2 className="notification-tray__title">Notifications</h2>
              {unreadCount > 0 && (
                <span className="badge badge--neutral" aria-label={`${unreadCount} unread`}>
                  {unreadCount} unread
                </span>
              )}
            </div>
            <div className="notification-tray__actions">
              {unreadCount > 0 && (
                <button
                  type="button"
                  className="btn btn--link btn--sm notification-tray__mark-all"
                  onClick={() => {
                    markAllReadMutation.mutate();
                  }}
                  disabled={markAllReadMutation.isPending}
                >
                  Mark all as read
                </button>
              )}
              <button
                type="button"
                className="btn btn--ghost btn--sm notification-tray__close"
                aria-label="Close notifications tray"
                onClick={() => {
                  setIsOpen(false);
                  buttonRef.current?.focus();
                }}
              >
                <Icon name="close" size={14} />
              </button>
            </div>
          </div>

          <div className="notification-tray__content">
            {notificationsQuery.isPending && (
              <div className="notification-tray__state">
                <LoadingState label="Loading notifications..." />
              </div>
            )}

            {notificationsQuery.isError && (
              <div className="notification-tray__state">
                <ErrorState
                  message={errorMessage(notificationsQuery.error, "Could not load notifications.")}
                  onRetry={() => {
                    void notificationsQuery.refetch();
                  }}
                />
              </div>
            )}

            {notificationsQuery.isSuccess && items.length === 0 && (
              <div className="notification-tray__state">
                <EmptyState message="No notifications yet." />
              </div>
            )}

            {notificationsQuery.isSuccess && items.length > 0 && (
              <ul className="notification-list" role="list">
                {items.map((item) => {
                  const typeInfo = formatNotificationType(item.type);
                  return (
                    <li key={item.id} className="notification-list__item">
                      <button
                        type="button"
                        className={`notification-item ${!item.read ? "notification-item--unread" : "notification-item--read"}`}
                        onClick={() => {
                          handleNotificationClick(item);
                        }}
                      >
                        <div className="notification-item__top">
                          <span
                            className={`badge badge--${typeInfo.tone}`}
                            style={{ fontSize: "var(--text-xs)" }}
                          >
                            {typeInfo.label}
                          </span>
                          <time
                            className="notification-item__time muted"
                            dateTime={item.created_at}
                          >
                            {formatDateTime(item.created_at)}
                          </time>
                        </div>
                        <div className="notification-item__heading">
                          {!item.read && (
                            <span className="notification-item__dot" aria-hidden="true">
                              ●
                            </span>
                          )}
                          <strong className="notification-item__title">
                            {!item.read && <span className="sr-only">(Unread) </span>}
                            {item.title}
                          </strong>
                        </div>
                        <p className="notification-item__body muted">{item.body}</p>
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
