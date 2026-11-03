# Model card: LightGBM fraud classifier

| | |
|---|---|
| Code | `ml/src/pbip_ml/fraud/training.py` (fit), `ml/src/pbip_ml/fraud/train.py` (CLI and MLflow logging), `ml/src/pbip_ml/fraud/inference.py` (load) |
| Artifact | `models/fraud/lightgbm.txt`, checksummed in `manifest.json` |
| `model_version` | not served alone; part of the hybrid `hybrid-lgbm-iforest-2` (`HYBRID_VERSION` in `train.py`; `-1` = the v2.1/v2.2 models) |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-029, TASK-033, TASK-038, TASK-039, TASK-071 |

## Intended use

Supervised probability that one **outgoing** transfer or bill payment is fraudulent. It is the main input
of the [hybrid combiner](hybrid-fraud-combiner.md). Its top-3 positive TreeSHAP contributions become the
MODEL reasons shown to staff.

**Out of scope:** scoring incoming credits or deposits (trained on outgoing transfers only since generator
v2.2); detecting structuring (F7) or dormancy (F8) as such; any customer-facing score; credit, AML or
eligibility decisions; use on real traffic without retraining on real labelled outcomes.

## Training data

- Synthetic, from `pbip_datagen` (TASK-023). Training export: `export --seed 42 --customers 3000 --months 6
  --out data/fraud` (`make fraud-model`). Held-out variant: `--seed 4242 --customers 1500 --months 6`.
  Source: `docs/ml/fraud-models.md` "Reproduce"; `docs/ml/model-evaluation-report-v1.md` §1.
- Split by time (train, validation, test), never at random. Generator v2.1 sizes: train 30,820,
  validation 10,273, test 10,274 rows with 115 frauds; held-out 26,009 rows with 268 frauds
  (`model-evaluation-report-v1.md` §2). From generator v2.2, outgoing rows only (§8).
- Seeds in code: training parameters `"seed": 42` and `IsolationForest(random_state=seed)` default 42
  (`training.py`); deterministic, one thread (report §1).
- Imbalance: `scale_pos_weight` = negatives / positives; early stopping on validation PR-AUC
  (`fraud-models.md` "LightGBM (supervised)").

## Features (9 columns, verified in `artifacts.py` `MODEL_FEATURES`)

`ratio_to_avg_amount`, `payee_age_seconds`, `velocity_10m`, `failure_count_24h`, `distinct_devices_7d`
(the TASK-024 features, `fraud/features.py` `FEATURE_NAMES`), plus `log_amount`, `hour_ist`, `new_device`
and `is_debit`. No demographic, location or identity field.

## Evaluation (copied from existing reports)

Current recipe, generator v2.3, `hybrid-lgbm-iforest-2` (`model-evaluation-report-v1.md` §9, MLflow run
`7b43ecc3827f4e8ebef35699f9a8f1ab`): PR-AUC 0.977 on the test period and 0.965 on the held-out seed, with early
stopping at iteration 81.

Hyper-parameters: TASK-084 ran a bounded Optuna search (`scripts/fraud_tune.py`: 30 trials, time folds inside
training and validation only). The best trial beat the current `LGBM_PARAMS` by +0.003 fold PR-AUC, which is
noise. In the full pipeline it was worse: PR-AUC 0.887 / 0.832, and the hybrid failed the 0.1% review bar on
held-out data. **The current parameters are kept** (§9). Global SHAP importance (mean |TreeSHAP|, test):
log_amount 0.39, payee_age_seconds 0.28, ratio_to_avg_amount 0.23, hour_ist 0.21, velocity_10m 0.05; the rest
are below 0.01. The chart is `docs/ml/img/fraud-shap-summary.png`.

Previous model, generator v2.2, outgoing transfers only (`model-evaluation-report-v1.md` §8, MLflow run
`394b64764f694164b3a1e0d356f4cf1d`):

| Set | Threshold | Precision | Recall | PR-AUC |
|---|---|---|---|---|
| Test | 0.817 (chosen on validation) | 0.903 | 0.894 | 0.962 |

Previous model, generator v2.1 (`model-evaluation-report-v1.md` §3.2):

| Set | Precision | Recall | F1 | PR-AUC | FPR |
|---|---|---|---|---|---|
| Test (threshold 0.968, chosen on validation) | 0.959 | 0.817 | 0.883 | 0.970 | 0.039% |
| Held-out seed 4242 | 0.965 | 0.817 | 0.885 | 0.960 | 0.031% |

- Latency, one row in memory: p50 0.48 ms, p95 0.72 ms (`fraud-models.md` "LightGBM (supervised)", the
  v2.0 run).
- Confusion matrices: v2.0 run in `fraud-models.md` "LightGBM (supervised)"; the served hybrid in report §3.1.
  No confusion matrix for LightGBM alone on v2.1/v2.2 is on main.

## Limitations

- Synthetic data with planted signatures: high scores mean "learns the planted patterns", not real-world
  performance (`fraud-models.md` "Limitations"; report §7).
- On generator v2.0 it learned a "new payee = fraud" shortcut. Fixed in v2.1 by adding legitimate
  first-time behaviour (GitHub issue #251, report §3.3); those rates are assumed, not observed (report §7).
- No location feature, so F4 (geo jump) is partly missed: hybrid recall at REVIEW 0.63 test / 0.69 held-out
  (report §8).
- Small positive counts: one miss moves test recall by about 0.9 points (report §7).
- Labels are exact and immediate, and the data has no adversarial adaptation (report §7).

## Fairness

Features are behavioural transaction signals only (list above). No gender, age, religion, caste,
ethnicity, marital status, health, location or income field (ADR-0015 decision 3). Not audited for
disparate impact: the synthetic population carries no protected attributes to audit against.

## Retraining and promotion

1. `make fraud-model` (Windows: `.\dev.ps1 fraud-model`) trains deterministically into `models/fraud` and
   logs a run to the MLflow experiment `pbip-fraud`.
2. `make promote-model` re-measures that run with `pbip_ml.fraud.evaluation` (never the numbers the run
   logged), registers it as a new version of `pbip-fraud` with the alias `challenger`, and moves the alias
   `champion` to it only if the gate passes: served-hybrid PR-AUC >= 0.80 on the test period (and on the
   held-out seed if given), p95 latency <= 50 ms, PR-AUC not below the champion's, p95 not above 1.5x the
   champion's. Exit code 0 promoted, 1 rejected, 2 could not run.
3. `make fraud-champion` exports the champion into `models/fraud` after checksum and loader checks;
   restart the API to serve it.

Source: `ml/src/pbip_ml/fraud/promotion.py`; `docs/ml/fraud-models.md` "Model registry and promotion".
LightGBM, the Isolation Forest and the hybrid parameters are one run and are promoted together.
