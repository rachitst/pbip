# ADR 0046: STRUCTURING (F7) and DORMANT_REACTIVATION (F8) rules: windows, thresholds and weights
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team

## Context
Blueprint section 8 lists F7 "structuring (many deposits just under a threshold): window sums near threshold" and
F8 "dormant account sudden activity: days since last activity" as SHOULD. Generator v2.2 (TASK-071) plants both, and
the evaluation report (`docs/ml/model-evaluation-report-v1.md` section 8) records that neither is detected for its own
reason: F7 is a pattern of incoming deposits, which the per-transfer scorer never sees (#274), and F8 is caught only
through takeover signals, because nothing measures the time since the last activity (#275).

Both issues propose ML work (an aggregate feature in both feature paths, a `days_since_last_activity` feature, a
retrain). That changes the `pbip_ml` feature contract and the trained model, and another task is changing calibration
and training at the same time (#396). This ADR takes the smaller step the blueprint also allows: two explainable
RULE-source rules in the baseline scorer (`fraud/baseline.py`), which the API serves as a floor under the hybrid score
(`max(rules, hybrid)`, ADR-0019). The ML feature work stays open on #274 and #275.

## Decision
1. **STRUCTURING** (source RULE) fires on a scored transfer when the number of movements on the debited account in
   the last W hours, each at least (100 - B)% of the threshold T and below it, reaches N. Counted: POSTED `TRANSFER`
   or `DEPOSIT` credits from accounts the customer does not hold (an inbound with no debit account counts too),
   POSTED `TRANSFER` debits to accounts the customer does not hold, and the scored transfer itself if it is in the
   band. Moves between the customer's own accounts are not counted, so shuffling money between them neither flags nor
   hides anything. Amounts at or above T are `LARGE_AMOUNT`'s job. The band check is integer (`amount x 100 >= T x
   (100 - B)`), so the boundary is exact. The reason cites the count, how many were received and sent, and the total.
   The snapshot stores `structuring_window_hours`, `structuring_threshold_minor`, `structuring_band_percent`,
   `structuring_received_count`, `structuring_sent_count`, `structuring_this_transfer`, `structuring_total_minor`,
   `structuring_transaction_ids` (first 20, comma-joined, as ADR-0035) and `structuring`.
2. **DORMANT_REACTIVATION** (source RULE) fires when the transfer is at least M and the account had no
   customer-induced activity for D days before it woke up. Activity is a POSTED `TRANSFER`, `DEPOSIT`,
   `BILL_PAYMENT` or `FD_BOOKING` on either side of the account. Fees, deposit interest, maturity payouts, reversals
   and loan postings are bank-initiated and do not count (the same distinction the RBI's inoperative-account
   guidance draws), so a monthly fee or interest credit cannot keep a dormant account looking alive. The account
   woke at its first activity in the last R days, or now with this transfer; the gap runs from the last activity
   before those R days (or from the account's opening, if there was none) to that wake-up. So a small first
   transfer does not hide a large second one minutes later, which a plain "days since last activity" would.
   The snapshot stores `dormancy_days`, `dormant_reactivation_days`, `dormant_gap_days`,
   `dormant_last_activity_at`, `dormant_woke_at` and `dormant_reactivation`.
3. **Defaults** (`core/config.py`, validated at boot; environment configuration, not runtime-overridable, because
   ADR-0031 excludes fraud scoring settings by construction):

   | Setting | Default | Bounds | Why |
   |---|---|---|---|
   | `FRAUD_STRUCTURING_THRESHOLD_MINOR` (T) | 10,000,000 (INR 1,00,000.00) | >= 20,000 | The platform's `LARGE_AMOUNT` threshold and the generator's synthetic reporting threshold |
   | `FRAUD_STRUCTURING_BAND_PERCENT` (B) | 10 | 1 to 50 | The generator's F7 deposits are 90 to 99.5% of T; "just under" |
   | `FRAUD_STRUCTURING_WINDOW_HOURS` (W) | 72 | 1 to 720 | The generator's F7 spreads 3 to 6 deposits over 3 days; the blueprint says "window sums" |
   | `FRAUD_STRUCTURING_MIN_COUNT` (N) | 3 | 2 to 50 | Two near-threshold amounts in three days happen (rent and a deposit); three is a pattern. With B <= 50 and N >= 2 the movements always total at least T, so no separate sum check is needed |
   | `FRAUD_DORMANCY_DAYS` (D) | 180 | 30 to 3,650 | The generator's dormancy and the blueprint's F8 |
   | `FRAUD_DORMANT_REACTIVATION_DAYS` (R) | 7 | 1 to 90 | Long enough to cover a test transfer before the real one; short enough that a returning customer is not flagged for weeks |
   | `FRAUD_DORMANT_MIN_AMOUNT_MINOR` (M) | 2,500,000 (INR 25,000.00) | >= 1 | The `NEW_PAYEE_LARGE_AMOUNT` line: smaller reactivations (a bill, a gift) are not worth a second factor |

4. **Weights.** +35 each: alone STEP_UP (a TOTP; customers without MFA are held for review, as for every STEP_UP);
   with `NEW_PAYEE` (+15) still STEP_UP (50); with `NEW_PAYEE_LARGE_AMOUNT` (+45) REVIEW (80), not BLOCK. The F8
   signature (dormant, a payee added minutes earlier, the maximum amount, a new device) adds `LARGE_AMOUNT` and
   `NEW_DEVICE_OR_NETWORK` and reaches BLOCK, which is intended. 45 (as `MULE_PASS_THROUGH`) was rejected: with
   `NEW_PAYEE_LARGE_AMOUNT` it is 90, a BLOCK for a structurer paying a new payee INR 25,000, which should be an
   analyst decision, not an automatic decline.
5. **Queries.** Read-only, no lock, no write, so the ledger lock order (AGENTS.md section 4) is unchanged and
   concurrent transfers see the same counts. STRUCTURING is one aggregate over the two existing indexes
   `ix_transactions_debit_account_id_created_at` and `ix_transactions_credit_account_id_created_at` (a BitmapOr of
   two range scans), plus one id query only when something matched. DORMANT_REACTIVATION is one statement of four
   scalar subqueries, each a `min`/`max` of `created_at` on one of those indexes, plus the account's `created_at` by
   primary key. No migration. Measured: with 1,000 near-threshold inbound transfers on the account, the whole
   baseline scorer stays under p95 100 ms (`test_scoring_stays_fast_with_1000_near_threshold_inbound_transfers`).
6. **Version.** `MODEL_VERSION` is `baseline-rules-0.6`.

### Measurement on the synthetic data (honest limits)
Generator v2.3.0 exports: seed 42 (3,000 customers x 6 months), held-out seed 4242 (1,500 x 6) and, for F8, seed 4242
(1,500 x 12; the 6-month exports cannot hold 180 days of dormancy). The models were retrained with the evaluation
harness (`python -m pbip_ml.fraud.train`, seed 42), which reproduced section 9 of the evaluation report (best
iteration 81, LightGBM test PR-AUC 0.977); the same model scores all three exports. An offline replica of both rules
was run over every outgoing row, and served risk was taken as in the API: rows where a new rule fires get
`max(hybrid, 35 per new rule + NEW_PAYEE / NEW_PAYEE_LARGE_AMOUNT / LARGE_AMOUNT points)`; other rows keep the hybrid
risk (the other API rules were not replicated; they can only add points). The export start, or the join date if later,
counts as an activity, because the customers were active before the data begins. Two readings: only the P2P transfers
count as movements and activity, or (conservatively for STRUCTURING, realistically for dormancy) the v1 income
credits count as received movements and every v1 row counts as activity. **Both readings gave identical numbers**:
the seed-42 export has 1,093 income credits in the band, but at most 6 per customer in 6 months, never 3 in 72 hours.

**The four bars (served risk; before = hybrid alone, after = with the two rules):**

| Bar | Bar value | Test (seed 42), before / after | Held-out 6 months, before / after | Held-out 12 months, before / after |
|---|---|---|---|---|
| Legitimate new-payee transfers held at REVIEW | <= 1% | 0.275% / 0.275% | 0.366% / 0.366% | 0.444% / 0.444% |
| Legitimate new-payee transfers asked for STEP_UP or more | <= 10% | 7.615% / 7.615% | 8.018% / 8.018% | 8.536% / 8.551% |
| All legitimate debits held at REVIEW | <= 0.1% | 0.040% / 0.040% | 0.051% / 0.051% | 0.066% / 0.066% |
| Recall at REVIEW, hybrid vs rules only | hybrid clearly better | 0.865 vs 0.202 / unchanged | 0.845 vs 0.270 / unchanged | 0.815 vs 0.292 / 0.832 vs 0.388 |

All bars still pass. Legitimate debits asked for STEP_UP or more: 0.837% to 0.856% (test, +2 rows), 1.040% to 1.048%
(held-out, +2), 1.087% to 1.095% (12 months, +4). No legitimate row was raised to REVIEW by the new rules.

**F8 (12-month held-out, 17 rows in 12 incidents):** `DORMANT_REACTIVATION` fires on 17 / 17 and on 0 legitimate
debits. Recall at REVIEW rises from 0.824 to 1.000 (STEP_UP was already 1.000). By construction a legitimate synthetic
customer is never dormant (churned customers stop everything), so the zero false positives say nothing about genuine
returning customers.

**F7 (whole period, every outgoing row):** the planted deposits are credits and are never scored. The rule can only
flag the structurer's next outgoing transfer within 72 hours of the third near-threshold deposit; the generator gives
F7 customers no outgoing leg, only their ordinary transfers. Incidents with such a transfer flagged: 9 / 31 (seed 42),
2 / 16 (held-out 6 months), 3 / 15 (held-out 12 months). Every legitimate-labelled row it fired on (11 of 50,230,
0.022%; 2 of 25,376; 4 of 49,965) is an ordinary transfer of an F7 customer inside the structuring window, so it is
the intended flag, counted as a false positive only because the label sits on the deposits. No customer outside F7 was
flagged in any export. The rule fired on 1 fraud row (12-month export).

**Behaviour baseline (TASK-080).** It scores `pbip_ml` cases with the `pbip_ml` scorer, which cannot import this rule;
in serving the rules only raise a score. Its canonical customer has twenty INR 2,000 debits in the last 20 days, so
neither rule can fire on any canonical case. Not re-run (#396 is retraining at the same time).

**Limits.** The generator has no legitimate inbound P2P transfers (#274 asks for them first), so the F7 false-positive
rate on legitimate deposits cannot be measured beyond the income reading above; real businesses with several receipts
just under INR 1,00,000 are the expected false positives and are not in the data. F7 detection is capped by how often
a structurer pays out within 72 hours, which the generator does not model. The F8 recall is on 17 rows, with a
signature (new device, payee added minutes earlier, maximum amount) that the takeover rules already catch, and the
window and thresholds match the generator's own parameters, so 17 / 17 partly measures the generator against itself.
No detection or false-positive rate is claimed for real traffic.

## Alternatives considered
* **The ML features #274 and #275 propose** (a windowed credit aggregate and `days_since_last_activity` in both
  feature paths, with a parity test and a retrain). Changes the `pbip_ml` feature contract (its own ADR) and the model
  being recalibrated in #396; and the generator has no legitimate inbound transfers, so an F7 feature would learn
  "any credit is fraud" (the train/serve skew section 8 of the report removed). Left open on the issues.
* **A Celery batch monitor over deposits for F7** (the other option in #274). Catches a structurer who never pays out,
  which this rule cannot, but it is a new background job and a new alert path (an ADR and threat-model work of its
  own). Left open on #274.
* **Weight 45 (as MULE_PASS_THROUGH).** BLOCK with `NEW_PAYEE_LARGE_AMOUNT`; see Decision 4.
* **Weight 60 (REVIEW alone).** Every flagged transfer would wait for an analyst, including a genuine returning
  customer; the measured legitimate firings above would all become holds.
* **Plain "days since last activity" for F8.** A small transfer first resets it; the reactivation window closes that
  gap at the cost of one more parameter.
* **Counting every posting as activity.** Interest and fees would keep a dormant account "active" forever.
* **Counting own-account moves for structuring.** A customer sweeping savings in three near-threshold steps would be
  flagged for moving their own money.

## Consequences
* Customer-visible: a customer whose account had three movements just under INR 1,00,000 in three days, or whose
  account was dormant for 180 days and now sends INR 25,000 or more, is asked for a TOTP (held for review without
  MFA); to a payee added in the last 24 hours with INR 25,000 or more, the transfer is held for review.
* Residual false positives: businesses that receive several payments just under INR 1,00,000 within three days; a
  genuine customer returning after six months with a large payment. Bill payments and scheduled transfers are scored
  by the same rules and are not special-cased.
* Evasion: amounts under 90% of the threshold, gaps longer than 72 hours, splitting across accounts, structuring
  without paying out (no deposit-side monitor); keeping a dormant account active for more than 7 days before the large
  transfer. Recorded in the threat model (T60, T61, section 7.1).
* Two more reads per scored transfer (three if any near-threshold movement matched). No schema change.
* Standing-approval conditions: (1) no security, auth, MFA, RBAC, BOLA or audit control is weakened: the rules can only
  raise a decision and reasons stay staff-only; (2) no money moves: read-only scoring, ledger untouched, lock order
  unchanged; (3) no migration; (4) no new service, dependency or outbound data; (5) every number above was measured,
  synthetic and labelled as such; (6) Blueprint section 8 F7/F8, issues #274/#275.
