import type { UserOut } from "../../api/client";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { PageHeader } from "../../ui/PageHeader";
import { RuntimeSettingsPanel } from "./AdminSettings";
import { CreateStaffUser, LockoutPanel } from "./AdminStaffUsers";
import "./Administration.css";

/** Every /api/v1/admin route admits ADMIN only (with an MFA-verified session). */
export function canAdminister(me: UserOut): boolean {
  return me.roles.includes("ADMIN");
}

/** TASK-108 (FR-05): staff users, lockouts and the allow-listed runtime settings (ADR-0031).
 * Showing the page is a convenience; the API enforces the role and the MFA session. */
export function Administration() {
  const me = useCurrentUser();
  if (!canAdminister(me)) {
    return (
      <div className="page">
        <PageHeader title="Administration" />
        <Alert tone="error">Access restricted: administration needs the Administrator role.</Alert>
      </div>
    );
  }
  return (
    <div className="page admin-page">
      <PageHeader
        title="Administration"
        description="Create staff users, clear sign-in lockouts and change the business limits that are safe to change at runtime. Every action is audited."
      />
      {!me.mfa_verified_session && (
        <Alert tone="warning">
          These tools need a session verified with an authenticator code; requests will be refused
          until you sign in again with your code.
        </Alert>
      )}
      <div className="admin-grid">
        <CreateStaffUser />
        <LockoutPanel />
      </div>
      <RuntimeSettingsPanel />
    </div>
  );
}
