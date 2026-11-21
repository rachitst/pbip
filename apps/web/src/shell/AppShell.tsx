import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import {
  Activity,
  BriefcaseBusiness,
  CalendarClock,
  ChartColumn,
  ChartPie,
  FileText,
  FlaskConical,
  KeyRound,
  Landmark,
  LayoutDashboard,
  ReceiptText,
  Scale,
  ScrollText,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Siren,
  TrendingUp,
  UserSearch,
  Users,
  Wallet,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { Logo } from "../brand/Logo";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";

export interface NavItem {
  to: string;
  label: string;
  /** Match only this exact path (for an area's index page). */
  end?: boolean;
}

/** One icon per destination, chosen by path so the navigation lists (routes.tsx) stay as they are.
 * Decorative: the link text is the accessible name. */
const NAV_ICONS: Record<string, LucideIcon> = {
  "/accounts": Wallet,
  "/payees": Users,
  "/bills": ReceiptText,
  "/scheduled": CalendarClock,
  "/statements": FileText,
  "/loans": Landmark,
  "/wealth": TrendingUp,
  "/insights": ChartPie,
  "/profile": ShieldCheck,
  "/staff": LayoutDashboard,
  "/staff/monitoring": ShieldAlert,
  "/staff/fraud-dashboard": ChartColumn,
  "/staff/fraud": Siren,
  "/staff/insights": UserSearch,
  "/staff/behaviour": Activity,
  "/staff/fraud-lab": FlaskConical,
  "/staff/compliance": Scale,
  "/staff/audit-log": ScrollText,
  "/staff/admin": Settings,
  "/staff/security": KeyRound,
};

function navIcon(item: NavItem): LucideIcon {
  // "Staff workspace" (from the customer area) is /staff without `end`; the staff Overview has it.
  if (item.to === "/staff" && !item.end) return BriefcaseBusiness;
  return NAV_ICONS[item.to] ?? LayoutDashboard;
}

interface AppShellProps {
  /** Who is signed in, rendered in the top bar. */
  session: ReactNode;
  nav: NavItem[];
  children: ReactNode;
}

/**
 * The one authenticated layout: sidebar (logo and navigation), top bar and main region, for customers
 * and staff. Below 1024px the navigation collapses behind a Menu button (CSS); it closes on
 * navigation and on Esc. After each navigation focus moves to the new page's heading so screen-reader
 * and keyboard users land on the content, not at the top of the document.
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
        <span className="app-brand app-brand--compact">
          <Logo size={28} />
        </span>
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
      <div className="app-sidebar">
        <span className="app-brand app-brand--sidebar">
          <Logo />
        </span>
        <nav id="app-nav" className="app-nav" aria-label="Main" data-open={menuOpen}>
          <ul>
            {nav.map((item) => {
              const ItemIcon = navIcon(item);
              return (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.end} className="app-nav__link">
                    <ItemIcon className="app-nav__icon" size={18} aria-hidden="true" />
                    {item.label}
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
      <main id="main" className="app-main" tabIndex={-1}>
        <div className="app-content">{children}</div>
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
