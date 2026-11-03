# Model card: hybrid fraud combiner

| | |
|---|---|
| Code | `ml/src/pbip_ml/fraud/hybrid.py` (combiner), `ml/src/pbip_ml/fraud/train.py` (fit, writes `hybrid_params.json`), `apps/api/src/pbip_api/modules/fraud/hybrid.py` (serving) |
| `model_version` | `hybrid-lgbm-iforest-1` (`HYBRID_VERSION`, `train.py`). When the models fail or exceed the budget, the API serves the baseline rules marked `<baseline version>+rules-only` (e.g. `baseline-rules-0.5+rules-only`) with a `MODEL_UNAVAILABLE` reason |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-031, TASK-032, TASK-033, TASK-038, TASK-039, TASK-080; ADR-0019 |

## Intended use

The fraud decision for outgoing transfers and bill payments when `FRAUD_SCORER=hybrid`: risk 0-100 and
ALLOW < 30 <= STEP_UP < 60 <= REVIEW < 85 <= BLOCK, with RULE / MODEL / ANOMALY reasons for staff.

How it combines (`docs/ml/fraud-models.md` "Hybrid decision" and "Serving"):

1. LightGBM probability, calibrated by isotonic regression fitted on validation.
2. A logistic regression stacks the calibrated probability, the Isolation Forest score and the rule points
   (weights fitted on validation).
3. Hard-rule floors: RAPID_ATO_TRANSFER at least REVIEW; NEW_PAYEE_LARGE_AMOUNT and VELOCITY_BURST at
   least STEP_UP.
4. Serving: `risk = max(baseline rules, hybrid)`, so the model can raise a decision but never lower it.
   A missing, failing or slow model (over `FRAUD_MODEL_BUDGET_MS`, default 50 ms) falls back to rules only,
   never to ALLOW-all.

**Out of scope:** incoming transfers and deposit structuring (F7, follow-up GitHub issue #274); dormancy as a signal
(F8, follow-up GitHub issue #275); any customer-facing score or reason; automatic account action beyond the
transfer state machine; use of the bands on real traffic before the weights are re-fitted on real labels.

## Training data

Same run as the components: synthetic `pbip_datagen` export seed 42 (3,000 customers, 6 months), held-out
seed 4242 (1,500 customers), split by time (`docs/ml/model-evaluation-report-v1.md` §1-§2). Calibration and
stacking are fitted on the validation period only.

## Features

LightGBM's 9 columns and the Isolation Forest's 4 (see their cards), plus the baseline rule points.
The served inputs are queried per transfer (30-day debits, payee age, failed sign-ins in 24 h) and go
through the same online feature code as training, checked by `test_served_row_equals_the_training_row`
(`fraud-models.md` "Serving").

## Evaluation (copied from existing reports)

**Current model, generator v2.2, outgoing transfers only** (`model-evaluation-report-v1.md` §8, MLflow run
`394b64764f694164b3a1e0d356f4cf1d`):

| Measure | Test | Held-out seed 4242 |
|---|---|---|
| Hybrid PR-AUC | 0.886 | 0.850 |
| Recall at REVIEW | 0.808 | 0.805 |
| False positives at REVIEW | 2 | 10 |
| Rules only, recall at REVIEW | 0.202 | - |

Test split by band (§8): STEP_UP precision 0.520, recall 0.865, FPR 0.82%; REVIEW precision 0.977, recall
0.808, FPR 0.020%; BLOCK precision 1.000, recall 0.789.

Acceptance bars (§8, test / held-out): legitimate new-payee transfers held for REVIEW 0.09% / 0.30%
(bar <= 1%); asked for STEP_UP 7.5% / 8.0% (bar <= 10%); all legitimate debits held at REVIEW 0.02% / 0.04%
(bar <= 0.1%). All pass.

Recall at REVIEW by scenario (§8, test / held-out): F1 0.93 / 0.75, F2 0.79 / 0.86, F3 1.00 / 1.00,
F4 0.63 / 0.69, F5 0.67 / 0.46, F6 outbound leg 1.00 / 0.90, F7 not applicable, F8 0.88 (12-month held-out,
17 rows).

**Previous model, generator v2.1** (§3.1, run `5de0a9dc7c534ed29701cd7703d4fb6a`), with confusion matrices:

| Data | Flagged at | Precision | Recall | F1 | FPR | TN | FP | FN | TP |
|---|---|---|---|---|---|---|---|---|---|
| Test | REVIEW (60) | 0.960 | 0.835 | 0.893 | 0.039% | 10,155 | 4 | 19 | 96 |
| Held-out | REVIEW (60) | 0.965 | 0.832 | 0.894 | 0.031% | 25,733 | 8 | 45 | 223 |

Hybrid PR-AUC v2.1: 0.860 test, 0.858 held-out (§3.1). The other bands are in §3.1.

**Latency:** hybrid score p50 17.0 ms, p95 24.7 ms in memory (`fraud-models.md` "Hybrid decision");
served end to end over 25 transfers p50 20.6 ms, p95 24.7 ms, max 29.2 ms (`fraud-models.md` "Serving").
Contract limit 100 ms. Report §5 has the v1.1 latency profile.

**Promotion gate re-measurement** (2 Nov 2026, `fraud-models.md` "Model registry and promotion"): PR-AUC
0.886 test / 0.850 held-out.

## Limitations

- **The hybrid does not beat LightGBM on PR-AUC** (v2.1: 0.86 vs 0.97, report §3.2). The rule floors push
  some legitimate transfers to STEP_UP (86 test false positives at STEP_UP on v2.1); that is the deliberate
  price of rules the model cannot lower.
- Stacking weights were fitted on synthetic validation data; the bands must be re-fitted on real labelled
  outcomes before they are relied on (`fraud-models.md` "Hybrid decision").
- Device signals are not on the payment path yet (TASK-095) (`fraud-models.md` "Serving").
- F7 is not detected; F8 is caught by takeover signals, not dormancy (report §8).
- All synthetic-data limitations in report §7: planted signatures, modelled (not observed) first-time
  behaviour, exact immediate labels, small positive counts, no adversarial adaptation.

## Fairness

No demographic input: the features are the components' behavioural signals and the baseline rule points.
No gender, age, religion, caste, ethnicity, marital status, health or income field (ADR-0015 decision 3).
`NEW_COUNTRY` (ADR-0014) and `IMPOSSIBLE_TRAVEL` (ADR-0034) use a coarse GeoIP country or city-level point
from the request, security signals, not demographic attributes of the customer; the models themselves never see
location. No disparate-impact audit is on main (no protected attributes in
the synthetic data).

## Explainability and audit

Every decision stores `risk_score`, `decision`, `reasons[]` and `model_version`, plus the full feature
snapshot (`model_probability`, `anomaly_score`, both risks) in `fraud_events`. Customers never see reasons
or scores (`fraud-models.md` "Serving").

## Retraining and promotion

`make fraud-model` -> `make promote-model` (registered model `pbip-fraud`, alias `challenger`, moved to
`champion` only if the measured gate passes) -> `make fraud-champion` (export with checksum checks) ->
restart the API. If MLflow is down, promotion and export refuse to run and the API keeps the last exported
champion; the API never needs MLflow (`fraud-models.md` "Model registry and promotion";
`ml/src/pbip_ml/fraud/promotion.py`). Details: [lightgbm-fraud.md](lightgbm-fraud.md#retraining-and-promotion).

`model_version` naming: `hybrid-lgbm-iforest-<n>` is a constant in `train.py`; the registry version number
(MLflow) and `champion.json` (version, run, manifest hash) identify the exact trained artifacts.
