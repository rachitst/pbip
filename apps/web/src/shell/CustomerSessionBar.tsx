import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { SignOutButton } from "./AppShell";
import { NotificationBell } from "./NotificationBell";
import { ThemeToggle } from "./theme";

export function CustomerSessionBar() {
  const profile = useQuery({
    queryKey: ["profile"],
    queryFn: api.profile,
    retry: false,
  });

  return (
    <aside className="session-bar" aria-label="Customer session">
      <div className="session-identity">
        {profile.isPending && <span className="muted">Loading session...</span>}
        {profile.isError && <span className="session-name">Signed in</span>}
        {profile.data && (
          <div className="session-details" aria-label="Authenticated customer">
            <span className="session-greeting">Customer: </span>
            <span className="session-name" aria-label="Customer name">
              {profile.data.full_name}
            </span>
            {profile.data.email && (
              <span className="session-email muted"> ({profile.data.email})</span>
            )}
            <span className="session-badge" aria-label="session status">
              Active session
            </span>
          </div>
        )}
      </div>
      <div className="session-actions">
        <NotificationBell />
        <ThemeToggle />
        <SignOutButton />
      </div>
    </aside>
  );
}
