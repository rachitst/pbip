# Fraud models: LightGBM classifier and Isolation Forest (TASK-029, TASK-030)

**Status:** trained offline and evaluated. Neither model is on the request path yet: the hybrid combiner (TASK-031), SHAP reasons (TASK-032) and API integration (TASK-033) come next.
**Code:**
- `ml/src/pbip_ml/fraud/training.py`: dataset, time split, models, metrics.
- `ml/src/pbip_ml/fraud/train.py`: CLI and MLflow logging.
- `ml/tests/test_fraud_training.py`.

## Reproduce

```bash
uv run python -m pbip_datagen export --seed 42 --customers 3000 --months 6 --out data/fraud
uv run python -m pbip_datagen export --seed 4242 --customers 1500 --months 6 --out data/holdout
uv run python -m pbip_ml.fraud.train --data data/fraud --holdout data/holdout --out models/fraud
```

The output is deterministic: two runs gave identical metrics. It writes `report.json`, `lightgbm.txt` and `isolation_forest.joblib`, and logs an MLflow run to experiment `pbip-fraud` (the local `mlruns/` fallback when no server is reachable).

## Data and split

- **Data:** TASK-023 labelled synthetic transfers, seed 42, 3,000 customers, 6 months: 36,403 transfers, 578 fraudulent (1.6%), covering scenarios F1–F6.
- **Features** (11): the five TASK-024 features (offline path, parity-tested against serving), log10 amount, hour of day (IST), new-device flag, and the debit flag.
- **Split by time:** each split is strictly later than the previous one.

  | Split | Dates | Rows | Fraud rows |
  |---|---|---|---|
  | Train | 1 Jan – 17 Apr 2025 | 21,842 | 329 |
  | Validation | 17 Apr – 22 May | 7,280 | 134 |
  | Test | 22 May – 27 Jun | 7,281 | 115 |
- **Held-out scenario variant:** a separately generated dataset (seed 4242, 1,500 customers: 18,320 transfers, 268 fraud) was scored with the frozen model and threshold.

## LightGBM (supervised)

- **Imbalance:** `scale_pos_weight` = negatives / positives (about 65).
- **Training:** early stopping on validation PR-AUC (best iteration 109).
- **Decision threshold:** 0.949, chosen to maximise F1 on **validation**. The test split was never used for any choice.

| Set | Precision | Recall | F1 | PR-AUC | FPR | FNR | TN | FP | FN | TP |
|---|---|---|---|---|---|---|---|---|---|---|
| Time-split test | 0.982 | 0.957 | 0.969 | 0.995 | 0.03% | 4.3% | 7,164 | 2 | 5 | 110 |
| Held-out seed 4242 | 1.000 | 0.944 | 0.971 | 0.988 | 0.00% | 5.6% | 18,052 | 0 | 15 | 253 |

- Recall by scenario, time-split test (held-out seed in brackets):

  | Scenario | Recall |
  |---|---|
  | F1 new payee + large | 14/14 (28/28) |
  | F2 velocity | 67/67 (131/131) |
  | F3 takeover | 7/7 (20/20) |
  | F4 geo jump | **5/8 (9/13)** |
  | F5 amount anomaly | **4/6 (13/24)** |
  | F6 | 13/13 (52/52) |
- Single-row scoring latency in memory: p50 0.48 ms, p95 0.72 ms.

## Isolation Forest (unsupervised anomaly)

- **Training data:** 100 trees on normal training debits only, over `ratio_to_avg_amount`, `velocity_10m`, `distinct_devices_7d` and `log_amount`.
- **Score:** the raw score is mapped to 0–1. The training median maps to 0 and the training 99.9th percentile to 1, clipped.
- **Explanation:** names the feature furthest from typical behaviour, scaled by its IQR, e.g. "velocity_10m is 6.00, usually about 0.00".

| Set (threshold 0.5) | Precision | Recall | F1 | PR-AUC | FPR | FNR |
|---|---|---|---|---|---|---|
| Time-split test | 0.092 | 0.365 | 0.147 | 0.299 | 5.8% | 63.5% |

- It flags **8/8 F4 and 5/6 F5** test cases, which are exactly the scenarios LightGBM partly misses. This is why the hybrid combiner (TASK-031) uses both.
- Single-row latency: p50 11.7 ms, p95 14.3 ms (target < 25 ms). With 200 trees it measured p95 25.8 ms, so 100 trees are used.

## Limitations (read before quoting these numbers)

- **The data is synthetic.** The generator plants each scenario with clear signatures, so near-perfect supervised scores mean "learns the planted patterns". They are not an estimate of real-world performance. Real fraud is adaptive, rarer, and labelled late and noisily.
- **There is no location feature yet.** Hence the F4 misses. Adding a country-change feature means adding it to both feature paths and their parity test first.
- **The test sets are small.** 115 fraud rows in the time-split test: one miss moves recall by about 0.9 points.
- **The threshold is tuned for F1.** The serving thresholds (STEP_UP, REVIEW, BLOCK) will be calibrated in TASK-031.

## Hybrid decision (TASK-031) and reason codes (TASK-032)

Code: `ml/src/pbip_ml/fraud/hybrid.py` and `ml/tests/test_fraud_hybrid.py`. The training CLI also writes `hybrid_params.json`.

1. The LightGBM probability is calibrated with isotonic regression, fitted on validation and stored as a lookup table.
2. A logistic regression **stacks** the calibrated probability, the anomaly score and the rule points. Its weights are fitted on validation; that is what "calibrated weights" means here.
3. Risk = round(100 × stacked probability).
4. **Hard-rule overrides** set floors: RAPID_ATO_TRANSFER → at least REVIEW (60); NEW_PAYEE_LARGE_AMOUNT → at least STEP_UP (30).
5. Actions: ALLOW < 30 ≤ STEP_UP < 60 ≤ REVIEW < 85 ≤ BLOCK.

**Fitted weights** (same run as above): intercept −6.84, model 8.31, anomaly 1.97, rules 1.70.

Time-split test (7,281 rows, 115 fraud):

| Scorer | Decisions (ALLOW / STEP_UP / REVIEW / BLOCK) | Recall at REVIEW+ | FPR at REVIEW+ | Recall at BLOCK |
|---|---|---|---|---|
| Hybrid | 7,169 / 0 / 9 / 103 | 0.957 | 0.03% | 0.878 |
| Rules only (fail-safe) | 7,123 / 141 / 10 / 7 | 0.148 | 0.00% | 0.061 |

- Rules-only recall at STEP_UP+ is 0.635 (FPR 1.19%). A rules-only score still steps up, reviews and blocks: the fail-safe never allows everything.
- **The hybrid does not beat LightGBM on this data.** The stacking regression gives the anomaly score only a small weight because validation fraud is already separated by the model. F4 and F5 recall stay at 5/8 and 4/6, and the scores are bimodal (no STEP_UP band in use).
- Honest reading: on synthetic data with planted signatures the supervised model dominates. The anomaly and rule layers earn their place in the fail-safe path and in the explanations, not in the headline metrics. Re-fit the weights on real labelled data before relying on the bands.

**Reasons:**
- every triggered rule (RULE);
- the top-3 positive TreeSHAP contributions of the LightGBM prediction, in words (MODEL). LightGBM's `pred_contrib` is exact (contributions sum to the raw score, tested), so no `shap` dependency is needed;
- the Isolation Forest reason when its score ≥ 0.5 (ANOMALY).

Example, a high-risk F5 transfer:
- RULE: "Amount is 7.3x this customer's 30-day average"
- MODEL: "Model: amount of INR 112,213.00"
- MODEL: "Model: amount is 7.3x the customer's 30-day average"
- ANOMALY: "Unusual: ratio_to_avg_amount is 7.32, usually about 1.00"

**Latency** (laptop, wall time, one row): hybrid score p50 17.0 ms, p95 24.7 ms (the contract's limit is 100 ms); SHAP reasons p95 3.5 ms (target < 30 ms).

**Not yet done:** the scorer is not on the request path. TASK-033 wires it into the transfer flow. That task must also show that `fraud_events` stores the snapshot and model version (the TASK-031 criterion) and that customer responses hide the reasons while staff endpoints show them (the TASK-032 criterion).
