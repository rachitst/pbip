import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { UserOut } from "../api/client";

/** Mirrors `RoleName` / `STAFF_ROLES` in apps/api/.../auth/models.py. */
export const STAFF_ROLES = [
  "RELATIONSHIP_MANAGER",
  "FRAUD_ANALYST",
  "COMPLIANCE_OFFICER",
  "ADMIN",
] as const;
export type StaffRole = (typeof STAFF_ROLES)[number];

export const ROLE_LABELS: Record<string, string> = {
  CUSTOMER: "Customer",
  RELATIONSHIP_MANAGER: "Relationship manager",
  FRAUD_ANALYST: "Fraud analyst",
  COMPLIANCE_OFFICER: "Compliance officer",
  ADMIN: "Administrator",
};

export function isCustomer(me: UserOut): boolean {
  return me.roles.includes("CUSTOMER");
}

export function isStaff(me: UserOut): boolean {
  return me.roles.some((role) => (STAFF_ROLES as readonly string[]).includes(role));
}

/** Which area a user belongs in. This only chooses what to SHOW: every staff and customer
 * endpoint enforces roles (and MFA for staff) on the server, so a wrong guess here can never
 * grant access, it can only show a page whose requests fail. */
export type Area = "customer" | "staff" | "none";

export function homeArea(me: UserOut): Area {
  if (isCustomer(me)) return "customer";
  if (isStaff(me)) return "staff";
  return "none";
}

/** The signed-in user, from GET /auth/me. The query cache is cleared on sign-out, so the next
 * person never inherits these roles. */
export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
}
