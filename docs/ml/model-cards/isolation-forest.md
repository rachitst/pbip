# Model card: Isolation Forest anomaly model

| | |
|---|---|
| Code | `ml/src/pbip_ml/fraud/training.py` (`train_isolation_forest`), `ml/src/pbip_ml/fraud/inference.py` (load) |
| Artifact | `models/fraud/isolation_forest.joblib`, checksummed in `manifest.json` |
| `model_version` | not served alone; part of the hybrid `hybrid-lgbm-iforest-1` |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-030, TASK-031, TASK-039 |

## Intended use

Unsupervised anomaly score (0-1) for one outgoing transfer, relative to normal training debits. It is a
second input of the [hybrid combiner](hybrid-fraud-combiner.md) and gives the ANOMALY reason ("velocity_10m
is 6.00, usually about 0.00") when its score is >= 0.5.

**Out of scope:** a standalone fraud decision (its precision is low, below); any customer-facing output;
anomaly detection on data other than outgoing transfers.

## Training data

- Same synthetic export as the LightGBM model: seed 42, 3,000 customers, 6 months
  (`docs/ml/fraud-models.md` "Reproduce"), time split as in `docs/ml/model-evaluation-report-v1.md` §2.
- Fitted on **normal training debits only**, 100 trees, `contamination="auto"`, `random_state=seed`
  (default 42) (`training.py` `train_isolation_forest`; `fraud-models.md` "Isolation Forest").
- Score mapping: the training median maps to 0 and the training 99.9th percentile to 1, clipped
  (`fraud-models.md` "Isolation Forest").

## Features (4, verified in `training.py` `ANOMALY_FEATURES`)

`ratio_to_avg_amount`, `velocity_10m`, `distinct_devices_7d`, `log_amount`. No demographic or location field.

## Evaluation (copied from existing reports)

| Set (threshold 0.5) | Precision | Recall | F1 | PR-AUC | FPR | Source |
|---|---|---|---|---|---|---|
| Test, generator v2.1 | 0.120 | 0.470 | - | 0.301 | 3.90% | `model-evaluation-report-v1.md` §3.2 |
| Test, generator v2.0 | 0.092 | 0.365 | 0.147 | 0.299 | 5.8% | `fraud-models.md` "Isolation Forest" (FNR 63.5%) |

- On the v2.0 test split it flags 8/8 F4 and 5/6 F5 cases, the scenarios LightGBM partly misses
  (`fraud-models.md` "Isolation Forest").
- Latency, one row: p50 11.7 ms, p95 14.3 ms (target < 25 ms); 200 trees measured p95 25.8 ms, hence 100
  trees (`fraud-models.md` "Isolation Forest").
- No separate §8 (generator v2.2) number for the Isolation Forest alone is on main.

## Limitations

- Low precision alone; it is useful as a complementary signal and in explanations, not as a classifier.
- In the stacked hybrid it carries a small weight (1.43 vs 8.55 for LightGBM, report §3.2), because
  validation fraud is already separated by LightGBM (`fraud-models.md` "Hybrid decision").
- "Normal" is defined by synthetic legitimate behaviour; real customers' spread will differ.
- All the synthetic-data limitations of report §7 apply.

## Fairness

Four behavioural transaction features only. No protected or demographic attribute (ADR-0015 decision 3).
Not audited for disparate impact (no protected attributes in the synthetic data).

## Retraining and promotion

Trained, logged, measured and promoted in the same run as LightGBM: `make fraud-model`, then
`make promote-model` (MLflow aliases `challenger` / `champion` on `pbip-fraud`), then `make fraud-champion`.
See [lightgbm-fraud.md](lightgbm-fraud.md#retraining-and-promotion) and `ml/src/pbip_ml/fraud/promotion.py`.
