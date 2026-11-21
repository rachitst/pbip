import { ShieldCheck, ShieldAlert, UserRound } from "lucide-react";
import { ROLE_LABELS } from "../../auth/session";
import { useCurrentUser } from "../../shell/MeContext";
import { PageHeader } from "../../ui/PageHeader";
import "./staff.css";

/** Staff landing page. It shows only what GET /auth/me returns; staff tools (fraud queue,
 * audit log, ...) are added as routes by their own tasks and appear in the navigation then. */
export function StaffHome() {
  const me = useCurrentUser();
  const staffRoles = me.roles.filter((role) => role !== "CUSTOMER");
  const SessionIcon = me.mfa_verified_session ? ShieldCheck : ShieldAlert;
  return (
    <div className="page staff-page staff-home">
      <PageHeader title="Staff workspace" />
      <section className="card" aria-labelledby="staff-session">
        <h2 id="staff-session" className="panel-title">
          <UserRound className="panel-title__icon" size={20} aria-hidden="true" />
          Your session
        </h2>
        <dl className="details">
          <dt>Signed in as</dt>
          <dd>{me.email}</dd>
          <dt>Roles</dt>
          <dd>{staffRoles.map((role) => ROLE_LABELS[role] ?? role).join(", ")}</dd>
          <dt>Authenticator</dt>
          <dd className="staff-home__mfa">
            <SessionIcon
              className={me.mfa_verified_session ? "ok" : "warn"}
              size={18}
              aria-hidden="true"
            />
            {me.mfa_verified_session
              ? "This session was verified with an authenticator code"
              : "This session was not verified with an authenticator code"}
          </dd>
        </dl>
      </section>
      <p className="muted staff-home__note">
        Staff tools appear in the navigation as they become available for your role.
      </p>
    </div>
  );
}
