# ADR 0035: MULE_PASS_THROUGH rule (F6): window, thresholds and weight
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
Blueprint section 8 lists F6 "mule pass-through (credit in, immediately out)" as a MUST. The TASK-028 threat-model
review found that nothing scores it (#144): a customer can receive money from other customers and send it straight on,
and the baseline rules (`fraud/baseline.py`) look only at the outbound transfer, its payee, the account's own debits and
the session. The ledger already has what the rule needs: `transactions.credit_account_id`, `type`, `status`, `amount`
and `created_at`. The issue fixes the shape of the rule; this ADR records the values that need a decision, because each
one trades caught mules against legitimate customers held for a second factor or for review.

## Decision
1. **Rule.** `MULE_PASS_THROUGH` (source RULE) fires on a scored outbound transfer when, for the debited account, the
   POSTED `TRANSFER` credits from accounts held by *other* customers in the last W minutes total I >= the minimum, and
   the outbound amount O >= R x I. DEPOSIT (the simulation's funding path), REVERSAL, FEE, every other type, transfers
   that are not POSTED, and moves between the customer's own accounts are not inbound. The reason cites the inbound
   total and count, minutes since the first inbound credit, the outbound amount and the percentage. The `fraud_events`
   snapshot stores `mule_window_minutes`, `mule_inbound_total_minor`, `mule_inbound_count`, `mule_ratio`,
   `mule_inbound_transaction_ids` (the first 20, comma-joined, because the `FraudScoreResult` contract holds scalars
   only; the count and total are always complete), `mule_pass_through` and, when it fires, `mule_earlier_episodes_30d`.
   These are staff-only, like every snapshot; the customer's response never carries reasons, scores or ids.
2. **Defaults** (`core/config.py`, validated at boot):

   | Setting | Default | Bounds | Why |
   |---|---|---|---|
   | `FRAUD_MULE_WINDOW_MINUTES` (W) | 60 | 1 to 1,440 | Blueprint F6 says "immediately out"; the issue proposes 60. See the measurement below for what a longer window would catch and cost |
   | `FRAUD_MULE_MIN_INBOUND_MINOR` | 1,000,000 (INR 10,000.00) | >= 1 | Small household transfers (splitting a bill, pocket money) passed on are not worth a second factor |
   | `FRAUD_MULE_MIN_RATIO_PERCENT` (R) | 80 | 1 to 100 | "Most of it" leaves; an integer percentage so the boundary is exact (`O x 100 >= R x I`, no float) |
   | `FRAUD_MULE_REPEAT_EPISODES` (N) | 2 | 1 to 100 | The third pass-through in 30 days is a pattern, not a coincidence |

   The settings are environment configuration, **not** runtime-overridable: ADR-0031 allow-lists three business
   limits only and excludes fraud scoring settings by construction, so they are not added there.
3. **Weight.** +45, so the rule alone reaches STEP_UP (30) and with `NEW_PAYEE` (+15) reaches REVIEW (60). With a
   repeat (N or more earlier episodes from the same account in 30 days, counted from the earlier transfers'
   `fraud_events` reasons) the weight is +60: REVIEW on its own. The rules are additive, so with
   `NEW_PAYEE_LARGE_AMOUNT` (+45, a new payee and INR 25,000 or more) the score is 90: BLOCK. That is the synthetic
   F6 signature (a payee added 10 minutes before forwarding 90-98% of the inflow), so it is intended, and it is the
   reason the minimum inbound and the ratio are not lower.
4. **Query and index.** One aggregate query (sum, count, first time) and, only if anything arrived, one query for up to
   20 ids, both on a new index `ix_transactions_credit_account_id_created_at` (migration 0025; the FK had no index).
   The episode count is a third query, run only when the rule fires. All are reads: no lock is taken and nothing is
   written, so concurrent transfers see the same inbound total and the ledger lock order (AGENTS.md section 4) is
   unchanged. Measured: with 1,000 inbound transfers on the account, the whole baseline scorer had p95 42.8 ms and
   median 18.8 ms over 30 calls on the shared laptop at load average 7.4 (`test_scoring_stays_fast_with_1000_inbound_transfers`).
5. **Version.** `MODEL_VERSION` is `baseline-rules-0.4`.

### Measurement on the synthetic data (honest limits)
An offline replica of the rule was run over the `make fraud-model` export (seed 42, 3,000 customers, 6 months; whole
period, not only the test split): 50,789 legitimate outgoing transfers, 4,212 of them to a payee added less than 24 hours
earlier, and 16 F6 forwards. The generator has no ordinary customer-to-customer credits; legitimate inflows are income.
So the rule was measured twice: counting only P2P credits, and (conservatively) counting every income credit as if it
came from another customer.

| Window | Legitimate debits fired (P2P only / with income) | Legitimate new-payee debits fired (P2P only / with income) | F6 forwards caught |
|---|---|---|---|
| 60 min (default) | 0 / 11 (0.022%) | 0 / 1 (0.024%) | 2 / 16 |
| 120 min | 0 / 20 (0.039%) | 0 / 3 (0.071%) | 3 / 16 |
| 360 min | 0 / 61 (0.120%) | 0 / 9 (0.214%) | 16 / 16 |

Effect on the hybrid bars (evaluation report v1, sections 3.3 and 8): the ML package has its own rule registry
(`pbip_ml/fraud/rules.py`) and does not contain this rule, so the trained model and its features are unchanged. In
serving, the API takes `max(baseline, hybrid)`, so the rule can only raise a decision. To measure that, the v2.2 models
were retrained with the evaluation harness (`python -m pbip_ml.fraud.train`, seed 42, 3,000 customers x 6 months,
held-out seed 4242, 1,500 x 6), which reproduced the report's numbers exactly, and each row where an offline replica of
the rule fires was given `max(hybrid, 45 + NEW_PAYEE / NEW_PAYEE_LARGE_AMOUNT / LARGE_AMOUNT points)` (the other API
rules were not replicated; they can only add points). New payee means added less than 3 days earlier, as in the report.

| Bar (test / held-out) | Bar | Before | After, P2P credits only | After, income counted as inbound |
|---|---|---|---|---|
| Legitimate new-payee transfers held at REVIEW | <= 1% | 0.09% / 0.30% | 0.09% / 0.30% | 0.09% / 0.34% |
| Legitimate new-payee transfers asked for STEP_UP or more | <= 10% | 7.49% / 7.99% | unchanged | unchanged |
| All legitimate debits held at REVIEW | <= 0.1% | 0.020% / 0.039% | unchanged | 0.020% / 0.043% |
| Hybrid recall at REVIEW (test) vs rules only | hybrid clearly better | 0.808 vs 0.202 | unchanged | unchanged |

The rule fired on 2 test rows and 2 held-out rows (3 legitimate, all only under the income reading); the one fraud row
it fired on was already at REVIEW. All bars still pass. With W = 360 the whole-period count above (0.12% of legitimate
debits under the income reading) shows the all-legitimate bar could be at risk; this was not re-measured on the splits.

Limits: the generator's F6 forward waits 15 to 300 minutes after the last credit, a choice of the generator, not
evidence about real mules; tuning W to catch 16 / 16 would fit the rule to its own test data. There is no labelled
real-world data, and the synthetic data has no shared households or marketplace sellers who legitimately receive and pay
on. No detection or false-positive rate is claimed beyond the synthetic numbers above.

## Alternatives considered
* **W = 360 minutes.** Catches every synthetic F6 forward but, under the conservative reading, could fire on 0.12% of
  legitimate debits, enough to breach the all-legitimate REVIEW bar; and it fits the generator's own parameter. Kept as
  a configuration change an operator can make with evidence.
* **Weight 30 (STEP_UP alone).** With `NEW_PAYEE` it would reach only 45 (STEP_UP), against the issue's requirement
  that a pass-through to a new payee is held for review.
* **Adding the rule to `pbip_ml/fraud/rules.py` as well.** Changes the model's rule-points feature and needs a retrain
  and a new evaluation, and the ML features have nothing to compute it from: they are built from the customer's
  debits only (`pbip_ml/fraud/features.py`), and the API passes the model debits only. Left to a separate task if the
  model should learn from it.
* **Redis counters for inbound totals (TASK-034 style).** Faster on paper, but the PostgreSQL read is already well
  under budget and is exact; a cache would add a staleness window to a money decision.
* **Payee age or verified-payee status as an exclusion.** The issue lists them as later controls; not done here.

## Consequences
* A forward of most of a recent inflow from other customers is asked for a second factor (customers without MFA are
  held for review, as for every STEP_UP); to a new payee it is held for review; to a payee added in the last 24 hours
  with INR 25,000 or more it is blocked.
* Residual false positives: shared households and marketplace sellers who receive and then pay on within the window,
  above the minimum and the ratio. They see a TOTP prompt, or a hold to a new payee. Bill payments and scheduled
  transfers are scored by the same rules and are not special-cased; their owners should confirm the expected behaviour.
* Evasion: a mule can wait longer than W, forward less than R, or split across accounts. The single-hop rule does not
  see rings (F11, TASK-072). Recorded in the threat model (T25, section 7.1).
* One more indexed read per scored transfer (two if anything arrived, three if the rule fires).
