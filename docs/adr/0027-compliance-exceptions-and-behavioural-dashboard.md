# ADR 0027: Compliance exceptions from existing facts, and an aggregate-only behavioural dashboard
- Status: Accepted (product owner, 2026-11-02, with the two conditions under "Owner conditions")
- Date: 2026-11-02
- Deciders: Architecture Team

## Context
UC-12 (requirements section 5) asks for two internal dashboards that did not exist:

* **Compliance & Risk** (TASK-100, #183): system-generated alerts, exception management (blocked transfers,
  lockouts, audit-chain verification failures) with acknowledge and resolve actions that are audited with actor and
  reason, and regulatory indicators (reviews pending past SLA, unresolved exceptions).
* **Behavioural Analytics** (TASK-101, #184): segment distribution, spending patterns by segment, product affinity
  (holdings and recommendation interactions) and engagement (clickstream), filterable by segment and period, with no
  per-customer drill-down without the RM role.

The facts already exist: `fraud_events` (decision `BLOCK`), `security_events` (`ACCOUNT_LOCKED`), the audit hash
chain (`AuditService.verify_chain`), `fraud_alerts`, `customer_segments`, ledger entries, accounts, loans and
`clickstream_events`. What was missing is a place to keep an exception's workflow state, and the queries.

## Decision
1. **Exceptions are derived, not copied.** `modules/compliance/exceptions.py` reads blocked payments and lockouts from
   their owning tables in one `UNION ALL`, and re-verifies the audit chain on each request (the same O(n) walk as
   `GET /compliance/audit-logs/verify`). A chain failure is one exception keyed by the first failing sequence number;
   it is a standing condition, so it is listed whatever the period.
2. **Workflow state lives in the audit log, not a new table.** Acknowledge and resolve append an audit row
   (`COMPLIANCE_EXCEPTION_ACKNOWLEDGED` / `..._RESOLVED`, resource `compliance_exception`, id `KIND:ref`, details
   `{kind, ref, previous_status, reason}`), and the status shown is read back from those rows (first row per action).
   So "every action is audited with actor and reason" holds by construction, the history cannot be edited (append-only
   trigger and hash chain), and no migration is needed. The read-and-append runs under the audit-chain advisory lock
   (`AuditService.lock_chain`, re-entrant within the transaction), so concurrent officers cannot both acknowledge.
   OPEN -> ACKNOWLEDGED -> RESOLVED, and OPEN -> RESOLVED; anything else is 409. Unknown or malformed exceptions are 404.
3. **Service levels are the bank's own constants**: a fraud review should be decided within 24 hours and an exception
   acknowledged within 24 hours (`REVIEW_SLA`, `ACKNOWLEDGE_SLA`). No regulator in the requirements prescribes these
   numbers; the API returns them with the indicators so the screen states them.
4. **Behavioural analytics are aggregates only.** `modules/analytics/behaviour.py` returns counts, sums, averages and
   shares per segment, never a customer id. The route is `GET /api/v1/insights/behaviour` behind the existing RM gate
   (`RELATIONSHIP_MANAGER`, `ADMIN`, MFA), so the route-policy test that guards every insights route covers it. The
   per-customer view stays the RM insights page; the dashboard links a segment to it only for roles that may open it.
5. **Cross-module reads follow the TASK-099 precedent**: reporting queries read other modules' models directly, as
   `fraud/monitoring.py` and `analytics/repository.py` already do; they never write to them.

## Owner conditions (2026-11-02) and how they are met
1. **Derive the current state from the latest event per exception with an indexed query, with no
   full audit-log scan per page load.** An exception's state is read with `DISTINCT ON` over the
   `exception`-type audit rows only, which uses the existing `ix_audit_logs_resource (resource_type,
   resource_id)` index. The hash-chain check that produces `AUDIT_CHAIN_FAILURE` is the only full
   read. It now runs at most once per `CHAIN_CACHE_TTL_SECONDS` (300 s, Redis) however many
   officers load the page (`chain_status`), so a tampered row shows up within 5 minutes.
   `GET /audit-logs/verify` still verifies on demand. Test:
   `test_page_loads_reuse_the_cached_chain_verification` (3 loads, 1 verification; it fails
   without the cache).
2. **A resolved exception can only change again through a new audited event.** RESOLVED is
   terminal: acknowledging or resolving it again is a 409, checked while holding the audit-chain
   lock. Every change is itself an append-only audit row, written in the same transaction, so no
   state exists outside the audit trail. Tests:
   `test_acknowledge_then_resolve_is_audited_with_actor_and_reason` and
   `test_concurrent_acknowledgements_record_exactly_one`.

## Consequences
* No schema change. The exceptions screen costs one chain verification per load; with a very large audit log this
  should move to a scheduled verification whose result is stored (a background job, which would need its own ADR).
* If the chain is broken at sequence N and later tampered again further on, only N is reported until N is repaired,
  because verification stops at the first failure.
* Engagement and recommendation-click panels are only as good as the clickstream: the web app does not send events
  yet (US-30), so on the demo data those panels show their empty state rather than invented numbers.
* Spend is INR only (every customer account is INR) and excludes reversals and moves between the customer's own
  accounts; segment averages are the features the weekly job stored, so they are as old as the last run.

## Addendum (2026-11-05): system exceptions
Two kinds are added so section 5 "exception management" covers system failures, not only security and fraud events:
* `NOTIFICATION_DEAD_LETTER`: an outbox event that failed its last delivery attempt (ADR-0040);
* `SCHEDULED_TRANSFER_FAILURE`: an occurrence the scheduler recorded as missed, or an order it paused after a
  permanent failure (ADR-0023).

Both producers already write an audit row (`OUTBOX_EVENT_DEAD_LETTERED`; `SCHEDULED_TRANSFER_MISSED` and
`SCHEDULED_TRANSFER_AUTO_PAUSED`). The exception is that row: its reference is the audit sequence number, its time the
row's `created_at`, and its detail an allow-list of the row's fields (event type, attempts, error class, reason code,
date, failed attempts), never an outbox payload. Reading the audit log keeps the compliance module inside its own
tables, and the scan is narrowed by `resource_type` to `ix_audit_logs_resource`. Workflow, audit and 409 rules are
unchanged. Refused postings (a payment the ledger declines) are not listed: by design they leave no state behind
(AGENTS.md section 4), and a payment the fraud engine refuses is already a `BLOCKED_TRANSFER`. No schema change.
