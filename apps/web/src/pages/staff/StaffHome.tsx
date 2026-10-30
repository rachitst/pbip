import { ROLE_LABELS } from "../../auth/session";
import { useCurrentUser } from "../../shell/MeContext";
import { PageHeader } from "../../ui/PageHeader";

/** Staff landing page. It shows only what GET /auth/me returns; staff tools (fraud queue,
 * audit log, ...) are added as routes by their own tasks and appear in the navigation then. */
export function StaffHome() {
  const me = useCurrentUser();
  const staffRoles = me.roles.filter((role) => role !== "CUSTOMER");
  return (
    <div className="page">
      <PageHeader title="Staff workspace" />
      <dl className="details">
        <dt>Signed in as</dt>
        <dd>{me.email}</dd>
        <dt>Roles</dt>
        <dd>{staffRoles.map((role) => ROLE_LABELS[role] ?? role).join(", ")}</dd>
        <dt>Authenticator</dt>
        <dd>
          {me.mfa_verified_session
            ? "This session was verified with an authenticator code"
            : "This session was not verified with an authenticator code"}
        </dd>
      </dl>
      <p className="muted">
        Staff tools appear in the navigation as they become available for your role.
      </p>
    </div>
  );
}
