# ADR 0057: Behavioural profile from consented clickstream, with sessions and product interests
- Status: Accepted (self-accepted under the standing approval: no money path, no fraud decision, no new service
  or dependency; the privacy choices take the safer option; logged for the owner's morning review)
- Date: 2026-11-04
- Deciders: Architecture Team (UC-11, section 5 "Behavioural Analytics Dashboard", FR-51)

## Context
UC-11 lists the data captured (page visits, clickstream, **session duration**, **product interests**, spend and
income patterns) and its outcome, **a customer behavioural profile**. Section 5's dashboard supports segmentation,
spending patterns, **product affinity analysis** and **user engagement metrics**. Before this decision:

* the web tracker (TASK-112) sent page visits and five interactions with a random `session_id` per signed-in visit,
  and the API (TASK-055, ADR-0055) stored them with no IP address, user agent or free text;
* the staff dashboard (TASK-101) counted sessions as distinct (customer, session id) pairs, so a tab left open all
  day was one session, and a session ended at its last click;
* `product_clicked` was only counted per product; nothing derived an interest per customer;
* there was **no recorded analytics consent**: the tracker followed the browser's Global Privacy Control signal
  only (`docs/analytics/clickstream-events.md`). ADR-0015's consent covers the date of birth for recommendations
  and nothing else.

## Decision

### 1. Recorded analytics consent, off by default
* `customers.analytics_consent` (boolean, default **false**), `analytics_consent_updated_at` and
  `analytics_consent_policy` (`ADR-0057-v1`). Every decision, on or off, stores its time and policy version; CHECK
  constraints make a grant without a time impossible. No existing customer is assumed to agree (no backfill).
* `GET`/`PUT /api/v1/customers/me/analytics-consent` (own customer only, `{"granted": bool}`, strict). Each change
  is audited (`ANALYTICS_CONSENT_GRANTED` / `ANALYTICS_CONSENT_WITHDRAWN`, policy version and purpose) in the same
  transaction; repeating the current decision writes nothing. The customer row is locked `FOR UPDATE`.
* The setting is on the customer's Profile page. The tracker collects only when the recorded consent is on **and**
  the browser sends no Global Privacy Control signal.

### 2. Without consent telemetry is **not recorded** (chosen over "recorded but excluded")
`POST /api/v1/events/batch` reads the consent with `SELECT ... FOR SHARE` and, when it is off, answers 202 with
`accepted: 0` and `not_recorded: n` and stores nothing. **Withdrawal erases** the customer's clickstream events and
stored profile in the same transaction as the decision (the audit row records how many events were erased).
`FOR SHARE` waits for a concurrent consent change to commit, so a batch can never be stored after the withdrawal
that erased the events.

Why not "recorded but excluded from profiles": it keeps personal behavioural data the customer refused, every
reader (dashboard, job, future features) would have to remember the filter, and withdrawal would not remove what
was collected. Not recording is data minimisation enforced at the single write path. Cost: engagement figures
cover consenting customers only, and the dashboard says how many those are (`consenting_customers`). Events stored
before this decision (TASK-112 recorded without a consent) are left in place for retention by partition, but every
reader (profile job, dashboard engagement and clicks) uses consenting customers only, so they are not used.

### 3. Sessions and duration
A session is the events of one `session_id`, ordered by time (out-of-order arrival is normal: batches are sent
every 10 s and on page hide), **split wherever more than 30 minutes pass without an event** (the conventional
web-analytics inactivity timeout). Duration is last event minus first event; a one-event session lasts 0 s. The
same rule runs in Python for the profile (`analytics/profiling.py`) and in SQL with window functions for the
dashboard (`behaviour.py`); a test checks both give the same numbers.

To measure time on a page instead of stopping at the last click, the tracker sends a **`session_heartbeat`**
event once a minute while the customer area is visible and the customer interacted in the last 5 minutes. It has no
properties, goes through the same batch (25-event cap, ADR-0055) and rate limits (10 batches a minute per tab, 120
per minute per IP at the API), and adds at most one event a minute. Heartbeats extend a session but are not counted
as interactions; a run of heartbeats alone is no session.

### 4. Product interests
Per customer and product category (`ProductType`: LOAN, SAVINGS, INVESTMENT, INSURANCE) the score is the sum of
`weight * 0.5 ** (age_days / 14)` over the last 90 days: a recommendation card opened (`product_clicked` of a
catalogue product, its type) weighs 3, a loan calculator used (LOAN) 2, a product page visited (`/loans`: LOAN;
`/wealth`, `/wealth/compare`: INVESTMENT) 1. An unknown product id is ignored; a future-dated event counts as now.
The weights and the 14-day half-life are a documented heuristic, not a trained model, and are shown as such.

### 5. The profile
* A daily Celery beat job (`pbip.analytics.build_behaviour_profiles`, 03:40 UTC, advisory-locked, idempotent)
  writes, for **consenting customers only**, `customer_behaviour_profiles` (sessions, mean duration, page views,
  interactions, last active, 90-day window) and `customer_product_interests` (category, score, signals, last
  signal). It deletes the profile of anyone whose consent is off (a safety net behind the erasure on withdrawal).
* Segment (weekly job, ADR-0012), spend and income (ledger, ADR-0043) are **read at request time** and not copied,
  so they are never staler than the rest of the analytics.
* `GET /api/v1/analytics/behaviour-profile/me` returns the caller's own profile.
  `GET /api/v1/insights/customers/{id}/behaviour-profile` is behind the existing RM gate (RELATIONSHIP_MANAGER or
  ADMIN, MFA); an RM reaches only assigned customers (ADR-0032), any other id is 404. Without consent both return
  `{"analytics_consent": false, "profile": null}`: staff learn only that there is no consent.
* FRAUD_ANALYST and COMPLIANCE_OFFICER get no access: the profile is a marketing and relationship tool, and fraud
  scoring does not use it (no fraud-decision change). PBIP has no general "analyst" role; adding one is a separate
  decision.

### 6. Dashboard
"Behavioural analytics" gains product interest by category across profiled customers and, per day, sessions and
mean session length next to the active customers already shown. Aggregates only, as before (ADR-0027).

## Alternatives considered
* **Record without consent and exclude from profiles:** rejected, see 2.
* **Consent on by default (opt-out):** rejected; behavioural profiling is not needed to provide banking, so it
  needs an explicit choice (as ADR-0015 did for the date of birth).
* **Compute the profile on every read:** rejected for the staff aggregates (one query per customer); the job also
  keeps request cost flat. The 24-hour staleness of engagement is shown (`computed_at`).
* **Session start/end events instead of heartbeats:** an end event is lost when a tab is killed or the network
  drops, and a start event duplicates the first page view. A heartbeat degrades gracefully: losing one shortens a
  session by at most a minute.
* **A sessions table:** not needed at this scale; sessions are derived from the events, which keep the source.

## Consequences
* Telemetry and the engagement panels are empty until customers opt in; demo customers must turn analytics on in
  Profile. The latency script grants consent for its benchmark customer so it still measures the insert.
* One extra indexed `SELECT ... FOR SHARE` per events batch (the customer row is already cached by authentication).
* Migration 0030 adds three columns to `customers` and two tables; its downgrade drops them (the decisions stay in
  the audit log).
* Threat model row T70.
