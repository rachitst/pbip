import { useQuery } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "./api/client";
import { useAuth } from "./auth/AuthContext";
import { Accounts } from "./pages/Accounts";
import { Login } from "./pages/Login";
import { Profile } from "./pages/Profile";
import { Button } from "./ui/Button";
import { Icon } from "./ui/Icon";

export type Theme = "light" | "dark";
const THEME_STORAGE_KEY = "pbip-theme";

function savedTheme(): Theme | null {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : null;
  } catch {
    return null; // storage can be unavailable or restricted (private browsing)
  }
}

function systemTheme(): Theme {
  try {
    if (typeof window.matchMedia === "function") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
  } catch {
    // matchMedia can be missing in some environments (jsdom)
  }
  return "light";
}

interface ThemeContextValue {
  theme: Theme;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/** Owns the theme for the whole app, including the sign-in page, so it cannot flip after login.
 *  The OS preference is followed until the user toggles; only an explicit choice is saved. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => savedTheme() ?? systemTheme());

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Storage might be unavailable in private browsing; the choice still applies this session
    }
    setTheme(next);
  }, [theme]);

  const value = useMemo(() => ({ theme, toggleTheme }), [theme, toggleTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside ThemeProvider");
  return context;
}

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  return (
    <Button
      variant="ghost"
      size="sm"
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      onClick={toggleTheme}
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} />
      {theme === "dark" ? "Light" : "Dark"}
    </Button>
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
        <ThemeToggle />
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            void logout();
          }}
        >
          Sign out
        </Button>
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
  return (
    <ThemeProvider>
      <AuthenticatedOrLogin />
    </ThemeProvider>
  );
}

function AuthenticatedOrLogin() {
  const { status } = useAuth();
  if (status === "loading")
    return (
      <p className="center" role="status">
        Loading...
      </p>
    );
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
