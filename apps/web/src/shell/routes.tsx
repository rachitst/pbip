import { Suspense, lazy } from "react";
import type { ReactNode } from "react";
import { Navigate, Outlet } from "react-router";
import type { RouteObject } from "react-router";
import type { UserOut } from "../api/client";
import { homeArea, isCustomer, isStaff, useMe } from "../auth/session";
import type { Area } from "../auth/session";
import { CustomerSessionBar } from "./CustomerSessionBar";
import { ThemeToggle } from "./theme";
import { errorMessage } from "../lib/errors";
import { Accounts } from "../pages/Accounts";
import { NotFound } from "../pages/NotFound";
import { Profile } from "../pages/Profile";
import { AuditLog } from "../pages/staff/AuditLog";
import { StaffHome } from "../pages/staff/StaffHome";
import { Alert } from "../ui/Alert";
import { ErrorState, LoadingState } from "../ui/States";
import { AppShell, SignOutButton } from "./AppShell";
import type { NavItem } from "./AppShell";
import { MeContext, useCurrentUser } from "./MeContext";

// Recharts is large: load the Insights page (and the chart library) only when it is opened.
const Insights = lazy(() => import("../pages/Insights"));

const HOME_PATH: Record<Area, string> = { customer: "/accounts", staff: "/staff", none: "/" };

function customerNav(me: UserOut): NavItem[] {
  const items: NavItem[] = [
    { to: "/accounts", label: "Accounts" },
    { to: "/insights", label: "Spending & health" },
    { to: "/profile", label: "Profile & security" },
  ];
  if (isStaff(me)) items.push({ to: "/staff", label: "Staff workspace" });
  return items;
}

/** Staff tools are added here by their tasks (TASK-036, TASK-048, ...), each filtered by role. */
function staffNav(me: UserOut): NavItem[] {
  const items: NavItem[] = [{ to: "/staff", label: "Overview", end: true }];
  if (me.roles.includes("COMPLIANCE_OFFICER") || me.roles.includes("ADMIN")) {
    items.push({ to: "/staff/audit-log", label: "Audit log" });
  }
  if (isCustomer(me)) items.push({ to: "/accounts", label: "My banking" });
  return items;
}

function StaffSessionBar({ me }: { me: UserOut }) {
  return (
    <aside className="session-bar" aria-label="Staff session">
      <div className="session-identity">
        <span className="session-name">{me.email}</span>
      </div>
      <div className="session-actions">
        <ThemeToggle />
        <SignOutButton />
      </div>
    </aside>
  );
}

function AreaLayout({ area, children }: { area: Area; children: ReactNode }) {
  const me = useCurrentUser();
  if (area === "staff") {
    return (
      <AppShell session={<StaffSessionBar me={me} />} nav={staffNav(me)}>
        {!me.mfa_verified_session && (
          <Alert tone="warning">
            Staff tools need a session verified with an authenticator code.{" "}
            {me.mfa_enabled
              ? "Sign out and sign in again, entering your authenticator code."
              : "Your account has no authenticator set up; ask an administrator to enrol one."}
          </Alert>
        )}
        {children}
      </AppShell>
    );
  }
  if (area === "customer") {
    return (
      <AppShell session={<CustomerSessionBar />} nav={customerNav(me)}>
        {children}
      </AppShell>
    );
  }
  return (
    <AppShell session={<StaffSessionBar me={me} />} nav={[]}>
      {children}
    </AppShell>
  );
}

/** Loads GET /auth/me once per session; nothing below renders until the roles are known. */
function SessionRoot() {
  const me = useMe();
  if (me.isPending) return <LoadingState label="Loading your session..." />;
  if (me.isError) {
    return (
      <div className="card narrow">
        <ErrorState
          message={errorMessage(me.error, "Could not load your session.")}
          onRetry={() => {
            void me.refetch();
          }}
        />
        <SignOutButton />
      </div>
    );
  }
  return (
    <MeContext.Provider value={me.data}>
      <Outlet />
    </MeContext.Provider>
  );
}

function CustomerArea() {
  const me = useCurrentUser();
  if (isCustomer(me)) {
    return (
      <AreaLayout area="customer">
        <Outlet />
      </AreaLayout>
    );
  }
  if (isStaff(me)) return <Navigate to="/staff" replace />;
  return <OutsideArea />;
}

function StaffArea() {
  const me = useCurrentUser();
  if (isStaff(me)) {
    return (
      <AreaLayout area="staff">
        <Outlet />
      </AreaLayout>
    );
  }
  return <OutsideArea />;
}

/** A page the user's role cannot use, shown inside the user's OWN area as "not found". */
function OutsideArea() {
  const me = useCurrentUser();
  const area = homeArea(me);
  return (
    <AreaLayout area={area}>
      <NotFound homePath={HOME_PATH[area]} />
    </AreaLayout>
  );
}

export const routes: RouteObject[] = [
  {
    element: <SessionRoot />,
    children: [
      {
        element: <CustomerArea />,
        children: [
          { index: true, element: <Navigate to="/accounts" replace /> },
          { path: "accounts", element: <Accounts /> },
          {
            path: "insights",
            element: (
              <Suspense fallback={<LoadingState label="Loading spending & financial health..." />}>
                <Insights />
              </Suspense>
            ),
          },
          { path: "profile", element: <Profile /> },
        ],
      },
      {
        path: "staff",
        element: <StaffArea />,
        children: [
          { index: true, element: <StaffHome /> },
          { path: "audit-log", element: <AuditLog /> },
        ],
      },
      { path: "*", element: <OutsideArea /> },
    ],
  },
];
