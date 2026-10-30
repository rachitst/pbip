import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";

export interface NavItem {
  to: string;
  label: string;
  /** Match only this exact path (for an area's index page). */
  end?: boolean;
}

interface AppShellProps {
  /** Who is signed in, rendered in the header. */
  session: ReactNode;
  nav: NavItem[];
  children: ReactNode;
}

/**
 * The one authenticated layout: header, navigation and main region, for customers and staff.
 * Below 1024px the navigation collapses behind a Menu button (CSS); it closes on navigation and
 * on Esc. After each navigation focus moves to the new page's heading so screen-reader and
 * keyboard users land on the content, not at the top of the document.
 */
export function AppShell({ session, nav, children }: AppShellProps) {
  const location = useLocation();
  // The menu remembers the path it was opened on, so any navigation closes it without an effect.
  const [menuOpenAt, setMenuOpenAt] = useState<string | null>(null);
  const menuOpen = menuOpenAt === location.pathname;
  const firstRender = useRef(true);

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    const heading = document.querySelector<HTMLElement>("#main h1");
    (heading ?? document.getElementById("main"))?.focus();
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpenAt(null);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="app-header">
        <span className="app-brand">PBIP</span>
        <Button
          variant="ghost"
          size="sm"
          className="menu-button"
          aria-expanded={menuOpen}
          aria-controls="app-nav"
          onClick={() => {
            setMenuOpenAt(menuOpen ? null : location.pathname);
          }}
        >
          <Icon name={menuOpen ? "close" : "menu"} />
          Menu
        </Button>
        {session}
      </header>
      <nav id="app-nav" className="app-nav" aria-label="Main" data-open={menuOpen}>
        <ul>
          {nav.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} end={item.end} className="app-nav__link">
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" className="app-main" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}

export function SignOutButton() {
  const { logout } = useAuth();
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={() => {
        void logout();
      }}
    >
      Sign out
    </Button>
  );
}
