# ADR 0052: F7 deposit-side structuring monitor as an hourly beat job in the existing alert queue
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issue #274, follow-up to ADR-0046)

## Context
F7 structuring is several credits that each stay just under a reporting threshold (generator v2.2: 3-6 deposits
from different senders within 3 days, each 90-99.5% of ₹1,00,000). ADR-0046 added the `STRUCTURING` rule, but it runs
on the payment path only: it flags the account's next scored transfer. A structurer who sends nothing within the
window is never scored, so the deposits alone are never seen (ADR-0046 "Residual"; threat model T60). Issue #274
offers a windowed aggregate feature or a batch monitor over deposits; the feature cannot help the case above either,
because a feature is only computed when a payment is scored. A new background job is an architectural change
(AGENTS.md rule 5).

## Decision
1. **Detection core** (`modules/fraud/structuring.py`, pure, no I/O): a credit is in band when
   `amount x 100 >= T x (100 - B)` and `amount < T`; a pattern is at least N in-band credits on one account with the
   first and last at most W apart (inclusive). T, B, W and N are ADR-0046's `FRAUD_STRUCTURING_*` settings, so the
   rule and the monitor cannot drift apart. The search is leftmost-greedy: the earliest credit that starts a pattern
   takes every credit within W of it. Credits that already belong to an alert ("covered") are removed before the
   search, so the same credit never opens a second alert, however runs and windows overlap.
2. **What a credit is** (ADR-0046's convention): POSTED `DEPOSIT` credits, and POSTED `TRANSFER` credits from an
   account no holder of the receiving account also holds, on accounts that have a holder (customer accounts). Outgoing
   money is the per-transfer rule's job.
3. **The job** (`modules/fraud/structuring_monitor.py`, task `pbip.fraud.monitor_structuring_deposits`): hourly at
   :35 (away from the :05 maturity payout and the :20 drift run). It reads the credits of the last 2W (`LOOKBACK_WINDOWS
   = 2`), so every pattern is whole in at least one run, and processes each account with a pattern in its own session
   and transaction, like the step-up expiry job (ADR-0036): a per-account transaction-level advisory lock
   (`hashtextextended('fraud:structuring-monitor:<account>')`, taken by nothing else, so it cannot deadlock with the
   payment lock order), the credits and the covered set re-read under it, then for each pattern one `fraud_events` row,
   one `fraud_alerts` row and one `AuditService.record` (`FRAUD_STRUCTURING_ALERT_OPENED`, actor `scheduler`), and one
   commit. A failure rolls the account back (no partial alert) and the next run retries it; one account never stops the
   batch. Two workers running at once open one alert (tested with threads).
4. **Alerts use the existing queue.** The event is on the pattern's LAST credit: decision `REVIEW`, risk 60 (severity
   MEDIUM), model version `structuring-monitor-1`, one RULE reason `STRUCTURED_DEPOSITS` with the count, the span and
   the total, and a snapshot with the account id, count, total, first/last time, the settings and every credit id
   (comma-joined; these ids are what makes a credit covered, so none is dropped). Analysts list, open and resolve it
   like any alert. Resolving only labels it: the credits are already posted, nothing is held or reversed, and the
   decision sends no "transfer posted" notice to the sender (`review_alert` skips the notice when the transaction was
   POSTED before the decision).
5. **Nothing else reads it as a payment decision.** Two readers of `fraud_events` assumed one event per transaction and
   payment: the hybrid scorer's history join now joins only the customer's own event (a monitor event on a transfer a
   customer SENT would otherwise duplicate that debit in its velocity and average), and `/fraud/stats` leaves monitor
   events out of decision totals, hold rate and latency p95. Security monitoring and the alert queue include them, as
   they should.
6. **Metric**: `fraud_monitor_alerts_total{monitor="structuring_deposits"}`, exported from the worker through Redis
   (ADR-0039). The run log has counts only, never amounts or account numbers.
7. **Offline measurement**: `scripts/structuring_monitor_eval.py` replays the same `find_patterns` over a
   `pbip_datagen` export as the hourly job runs it (one run at the first hour boundary after each in-band credit, which
   gives the same alerts as running every hour) and reports F7 incidents flagged, the false-positive rate per
   legitimate credit, false alerts and the customers they name.

## Alternatives considered
- **A new `monitor_alerts` table and queue.** Cleaner separation from payment decisions, but a second queue, API and
  screen for analysts, and a migration, for the same triage work. Rejected: the existing queue already carries the
  score, reasons, snapshot and resolution, and the two readers that assumed "one event = one payment" are fixed.
- **A windowed aggregate feature only** (#274's first option). It is computed only when a payment is scored, so it
  cannot see a structurer who never pays out. The aggregate feature is a separate change (`pbip_ml` contract, its own
  ADR).
- **Sliding re-alerting** (a new alert whenever a window holds N credits). Every further deposit would re-alert the
  same credits. Rejected for coverage: a credit is alerted once; continued structuring needs N new credits to alert
  again, and the open alert already names the account.
- **Scan on every deposit (synchronously).** Adds a read to the deposit path and a fraud decision to a posting that
  has none today. Rejected.

## Consequences
- Analysts get an alert for a deposit-only structurer within about an hour of the pattern's last credit.
- A business that legitimately receives several amounts just under ₹1,00,000 within 72 h is alerted (once per N
  credits). The customer sees nothing: no hold, no message, no score.
- The job's cost is one indexed range read of near-threshold credits per run (`ix_transactions_credit_account_id_
  created_at`), plus one short transaction per flagged account.
- Residual: amounts under 90% of the threshold, gaps longer than W, and splitting across several accounts or banks
  evade it, as for the rule. Covered credits never re-alert, so 4 or 5 credits in one window give one alert.

## Standing-approval conditions
1. No security, auth, MFA, RBAC, BOLA or audit control is weakened: alerts are analyst-only through the existing
   gated routes, and every alert is audited in its own transaction.
2. No money movement: the job never calls the ledger; it takes only its own advisory lock, so the lock order contract
   is untouched; it is idempotent (coverage under the lock) and concurrency-tested.
3. No migration (existing tables only).
4. No new service, dependency or outbound data: an entry on the existing Celery beat.
5. Numbers are measured on synthetic data and labelled so (PR description); nothing is invented.
6. Within Blueprint section 8 F7 and issue #274.
