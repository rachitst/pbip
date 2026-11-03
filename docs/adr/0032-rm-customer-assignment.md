# ADR 0032: Relationship-manager-to-customer assignment
- Status: Accepted (under owner's standing approval, 2026-11-03; requested by the product owner for #79)
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
ADR-0022 left the RM insights scope at "every customer, as in TASK-056, because no assignment exists". Any
Relationship Manager (RM) with an MFA session could therefore open the churn signals, health score, 30-day
volume and outreach notes of every customer in the bank. The product owner asked for a portfolio: an RM
works on the customers assigned to them, and an administrator decides who is assigned to whom.

The routes concerned are the TASK-056/TASK-066 insights routes behind `rm_gate`
(`require_roles(RELATIONSHIP_MANAGER, ADMIN, mfa=True)`): the list, the detail and the outreach log. The
behavioural dashboard (`GET /api/v1/insights/behaviour`, ADR-0027) is aggregate-only and carries no customer id.

## Decision
1. **Table `rm_assignments`** (module `insights`, migration after main's head). One row per assignment period:
   * `id` UUID PK, `created_at`;
   * `customer_id` FK `customers` `ON DELETE CASCADE` (an assignment has no meaning without the customer; the
     same rule as `rm_interactions`);
   * `rm_user_id`, `assigned_by_user_id` and `unassigned_by_user_id` FK `users` `ON DELETE RESTRICT` (staff are
     disabled, never deleted, so the history keeps naming who did what);
   * `assigned_at`, `unassigned_at` (NULL while active). **History is kept**: unassigning or reassigning
     closes the active row (sets `unassigned_at` and `unassigned_by_user_id`) and inserts a new one. Nothing
     is deleted.
   * CHECKs: `unassigned_at IS NULL OR unassigned_at >= assigned_at`; `unassigned_at` and
     `unassigned_by_user_id` are both set or both NULL.
   * **One active RM per customer**: a partial unique index on `customer_id WHERE unassigned_at IS NULL`.
   * Indexes: `(customer_id, assigned_at)` for a customer's history, `(rm_user_id)` for "my customers" (and
     the FK), and one each on `assigned_by_user_id` and `unassigned_by_user_id` (FK checks on `users`).
2. **Concurrency.** Assign and unassign take a transaction-scoped advisory lock on the customer
   (`pg_advisory_xact_lock(hashtext('rm_assignments'), hashtext(customer_id))`, the two-key form, so it can
   never collide with the single-key per-user and per-customer locks) before reading the active row. Two
   administrators acting at once are serialised; the partial unique index is the database safety net.
   The audit-chain lock is taken after it, inside `AuditService.record`, and no path takes them in the
   other order.
3. **Who can assign: ADMIN with MFA only** (`require_roles(ADMIN, mfa=True)`, a dedicated `assignment_gate`
   in the insights router). An RM cannot assign customers to themselves.
   * `PUT /api/v1/insights/customers/{customer_id}/assignment` with `{"rm_user_id": ...}` assigns or
     reassigns. The target must be an ACTIVE user with the `RELATIONSHIP_MANAGER` role (else 422). Assigning
     the RM who is already assigned is a no-op (idempotent, no new row, no audit row).
   * `DELETE /api/v1/insights/customers/{customer_id}/assignment` ends the active assignment (204, also when
     there is none, so a retry is safe).
   * `GET /api/v1/insights/relationship-managers` lists active RMs (id and email) for the admin's picker.
   * An unknown customer is 404. Each change writes `RM_ASSIGNED` (details: `rm_user_id`,
     `previous_rm_user_id`) or `RM_UNASSIGNED` (details: `rm_user_id`) to the audit chain in the same
     transaction, with the admin as actor and the customer as the resource.
4. **What an RM sees: only their assigned customers.** The list is filtered before paging and counting,
   so `total` is the size of their portfolio. The detail and both outreach-log routes give **404** for a
   customer that is not assigned to the caller, the same answer as for a customer that does not exist (the
   BOLA rule used everywhere; a 403 would confirm the id exists). The scope is decided by
   `insights/policy.py` from the authenticated principal only; no route accepts an RM or staff id for scoping.
5. **What ADMIN sees: every customer**, including unassigned ones (a user holding both roles is treated as
   ADMIN). The list response carries `scope` (`ASSIGNED` or `ALL`) so the page can say "your customers";
   the detail carries `assigned_rm` (id, email, since) so the admin can see and change it.
6. **Unassigned customers are visible to ADMIN only.** Least privilege: before this ADR the default was
   "all RMs see all customers", which is exactly what the owner asked to end; keeping unassigned customers
   open to every RM would leave most of the bank in that state until someone assigns them. The cost is that a
   new customer is invisible to RMs until an admin assigns them; the admin view shows who is unassigned.
7. **Outreach notes after a reassignment** stay with the customer: the new RM sees the notes logged by the
   previous RM (the log is about the customer, ADR-0022), and the previous RM loses access to all of it.
8. **Behavioural dashboard unchanged.** It returns counts, sums and shares per segment with no customer id,
   so it is bank-wide context for every RM, as ADR-0027 decided; scoping it to a small portfolio would make
   small-segment aggregates closer to individual data, not further.

## Consequences
- One new table and migration; additive v1 contract change (`scope` on the list, `assigned_rm` on the
  detail, three admin routes). `make openapi` regenerates the web types.
- RMs see nothing until an administrator assigns customers; `scripts/seed_dev.py` assigns the demo customer
  `alice@demo.pbip` to `rm@demo.pbip` (idempotently, through the audited service) and leaves `bob@demo.pbip`
  unassigned, so the demo shows both views.
- The RM list still computes the bank-wide activity query and then filters by the portfolio (one extra
  indexed query). That is linear in customers, as before (ADR-0022 §2); push the filter into the query if
  the bank grows.
- No bulk assignment, no team or branch hierarchy, no temporary cover between RMs. These need a requirement
  first.
