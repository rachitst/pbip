# ADR 0047: Calibrate the fraud model on out-of-time folds, and regularise the stack less
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issue #396, TASK-084)

## Context
The hybrid fraud scorer (`pbip_ml.fraud.hybrid`, ADR-0019) calibrates the LightGBM probability with isotonic
regression and then stacks the calibrated probability, the Isolation Forest score and the rule points with a
logistic regression. Both were fitted on the validation period only.

With the 1,500-customer recipe that period holds 42 fraud rows. The isotonic fit was a 12-point step function, and
a small data change (generator v2.3, #276) moved the canonical APP scam from a 0.92 step onto a 0.40 step: raw
LightGBM 0.90, risk 30, STEP_UP instead of BLOCK. The 3,000-customer production recipe (106 validation positives)
scored it 93, but the same fragility sat under it.

A second, related effect: the stack used sklearn's default L2 strength (`C=1.0`). With few positives, that shrinks
the model weight, so even a well-calibrated probability of 0.8 could not reach BLOCK.

## Decision
1. **Cross-fitted calibration over time.** `training.out_of_time_scores` cuts the training period into 4
   chronological blocks. Block k (k = 1..3) is scored by a LightGBM trained on blocks 0..k-1 with the final model's
   parameters and number of trees. The isotonic calibration is fitted on these scores **plus** the final model's
   validation scores. Every score used comes from a model that never saw that row or anything later. Test and
   held-out data are never used. On the production recipe the calibration rests on 317 positives instead of 106.
2. **Stack `C = 10`** (`hybrid.STACK_C`). The stack is still fitted on validation only, because the Isolation
   Forest was trained on the training period's normal rows.
3. **`HYBRID_VERSION = hybrid-lgbm-iforest-3`**, so `fraud_events` rows of this model are told apart from `-2`.

`HybridParams` keeps the same JSON fields. The serving code, the fraud contract, the thresholds and the rule
floors are unchanged.

## Evidence (choice made inside train + validation only)
Calibrators and stacks were fitted on the training folds plus the first half of validation and scored on the
second half (forward in time):

| Recipe | Calibration | Calibration Brier | Hybrid log-loss, C=1 / 10 / 100 |
|---|---|---|---|
| 1,500 | isotonic, validation only (before) | 0.00226 | 0.0116 / 0.0116 / 0.0151 |
| 1,500 | Platt, validation only | 0.00224 | 0.0116 / 0.0115 / 0.0150 |
| 1,500 | isotonic, 4 out-of-time folds (chosen) | **0.00189** | 0.0104 / **0.0084** / 0.0100 |
| 1,500 | Platt, 4 out-of-time folds | 0.00214 | 0.0109 / 0.0095 / 0.0113 |
| 3,000 | isotonic, validation only (before) | 0.00124 | 0.0117 / 0.0086 / 0.0086 |
| 3,000 | Platt, validation only | 0.00118 | 0.0119 / 0.0088 / 0.0089 |
| 3,000 | isotonic, 4 out-of-time folds (chosen) | **0.00117** | 0.0103 / **0.0066** / 0.0068 |
| 3,000 | Platt, 4 out-of-time folds | 0.00129 | 0.0107 / 0.0073 / 0.0072 |

3 and 5 folds gave similar results. Beta calibration was close to Platt. The held-out numbers below confirm the
choice; they were not used to make it.

Production recipe (seed 42, 3,000 customers; test / held-out seed 4242), `-2` -> `-3`: hybrid PR-AUC 0.922 / 0.852
-> 0.945 / 0.887; recall at REVIEW 0.865 / 0.845 -> 0.923 / 0.894; legitimate transfers held at REVIEW 4 / 13 ->
4 / 15. All four bars still pass (`docs/ml/model-evaluation-report-v1.md` §10). Behaviour cases: APP scam 93 -> 95,
velocity burst 30, takeover 60, routine grocery 0. On the 1,500-customer recipe (157 calibration positives instead of
42) the APP scam goes from 30 (STEP_UP) to 64 (REVIEW) with the shipped code; the exploratory script, which cut the
folds marginally differently, gave 78. Those two numbers are themselves a sign that the small recipe stays sensitive.

## Consequences
- More fraud is held at REVIEW at nearly the same false-hold rate (held-out: 2 more legitimate holds in 25,376).
  This is a fraud-decision change, visible to customers only as more holds on fraud-like transfers.
- Training trains three extra, smaller LightGBM models (seconds on the production recipe). Serving cost is
  unchanged: the calibration is still one lookup table.
- The calibration is still isotonic, so it is still a step function. It now has 36 points on 317 positives
  instead of 14 points on 106. It is a mitigation, not a cure: the 1,500-customer recipe's APP scam is at 64,
  held for review but below the suite's 80 bar for the production recipe. That recipe is not served.
- The fold models see less data than the final model, so their scores are not exactly the final model's. sklearn's
  `CalibratedClassifierCV(ensemble=False)` makes the same trade-off.

## Alternatives considered
- **Platt or beta calibration on validation only.** 2-3 parameters, so no steps, but the evidence above shows worse
  hybrid log-loss than out-of-time isotonic, and the 1,500-customer APP scam only rose to 62-65.
- **Isotonic with a minimum bin size.** Not in sklearn, so it would be custom code to maintain, and it still uses
  only the validation positives.
- **Fit the stack on the out-of-time rows too.** The Isolation Forest was trained on the training period's normal
  rows, so their anomaly scores are in-sample and biased low. Cross-fitting the forest as well is more work than this
  fix needs.

## Standing-approval conditions
(1) No security, auth, RBAC, BOLA or audit change. (2) No money movement. (3) No schema change or migration.
(4) No new service, dependency or outbound data. (5) Every number above is measured and reproducible (seeded).
(6) Within the requirements' fraud-scoring scope (the model is retrained with the same features and thresholds).
