import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api/client";
import { useAuth } from "./auth/AuthContext";
import { Accounts } from "./pages/Accounts";
import { Login } from "./pages/Login";
import { Profile } from "./pages/Profile";

export type Theme = "light" | "dark";

export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === "undefined") return "light";
    try {
      const saved = localStorage.getItem("pbip-theme");
      if (saved === "light" || saved === "dark") return saved;
      if (typeof window.matchMedia === "function") {
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
    } catch {
      // Storage or matchMedia might be unavailable/restricted in some environments
    }
    return "light";
  });

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("pbip-theme", theme);
    } catch {
      // Storage might be unavailable in private browsing
    }
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));
  }, []);

  return { theme, toggleTheme };
}

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  return (
    <button
      type="button"
      className="theme-toggle"
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      onClick={toggleTheme}
    >
      {theme === "dark" ? "☀️ Light" : "🌙 Dark"}
    </button>
  );
}

export function CustomerSessionBar() {
  const { logout } = useAuth();
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
            <span className="session-name font-semibold" aria-label="Customer name">
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
        <ThemeToggle />
        <button
          type="button"
          className="secondary btn-signout"
          onClick={() => {
            void logout();
          }}
        >
          Sign out
        </button>
      </div>
    </aside>
  );
}

type Page = "accounts" | "profile";

const PAGES: { id: Page; label: string }[] = [
  { id: "accounts", label: "Accounts" },
  { id: "profile", label: "Profile & security" },
];

export function App() {
  const { status } = useAuth();
  if (status === "loading") return <p className="center">Loading...</p>;
  return status === "authenticated" ? <Shell /> : <Login />;
}

function Shell() {
  const [page, setPage] = useState<Page>("accounts");
  return (
    <div className="card">
      <header className="shell-header">
        <CustomerSessionBar />
        <nav className="row nav" aria-label="Main">
          <div className="tabs">
            {PAGES.map((item) => (
              <button
                key={item.id}
                type="button"
                className={page === item.id ? "tab active" : "tab"}
                aria-current={page === item.id ? "page" : undefined}
                onClick={() => {
                  setPage(item.id);
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        </nav>
      </header>
      <div className="shell-content">{page === "accounts" ? <Accounts /> : <Profile />}</div>
    </div>
  );
}
