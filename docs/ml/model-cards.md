# PBIP model cards: index (TASK-088)

The cards are in [`model-cards/`](model-cards/README.md). This page lists them for the viva: one table and a
short summary per model. All numbers, evidence and limitations are in the linked cards.

| Model | `model_version` | What it decides | Headline evaluation | Card |
|---|---|---|---|---|
| LightGBM fraud classifier | part of `hybrid-lgbm-iforest-3` | Fraud probability for an outgoing payment (input to the hybrid) | Test PR-AUC 0.977, held-out 0.965; TASK-084 tuning kept the current params (report §9) | [lightgbm-fraud.md](model-cards/lightgbm-fraud.md) |
| Isolation Forest anomaly model | part of `hybrid-lgbm-iforest-3` | Anomaly score and the ANOMALY reason | Test PR-AUC 0.301 alone (report §3.2). Complementary signal only | [isolation-forest.md](model-cards/isolation-forest.md) |
| Hybrid fraud combiner | `hybrid-lgbm-iforest-3` (demo still serves `-1`) | ALLOW / STEP_UP / REVIEW / BLOCK for transfers and bill payments | PR-AUC 0.945 test, 0.887 held-out; recall at REVIEW 0.923 vs 0.202 rules only (report §10) | [hybrid-fraud-combiner.md](model-cards/hybrid-fraud-combiner.md) |
| Balance forecast | `forecast-arima-2` | Nothing: shows a predicted balance with intervals | sMAPE mean 10.31%, 80% / 95% coverage 86.6% / 96.4% | [balance-forecast.md](model-cards/balance-forecast.md) |
| K-Means segmentation | none | Nothing: an analytics persona and a recommendation input | No evaluation report on main | [kmeans-segmentation.md](model-cards/kmeans-segmentation.md) |
| Product recommendation score | `rules-v1` | Nothing: orders eligible products | Not a trained model. No outcome labels to evaluate against | [recommendation-score.md](model-cards/recommendation-score.md) |

**Governance:** [Alignment with AI governance guidelines for financial institutions](model-cards/governance-alignment.md)
maps every model to RBI FREE-AI, the EU AI Act's high-risk obligations and SR 11-7 / PRA SS1/23. It covers
fairness, explainability, human oversight, fail-safe, data governance, monitoring and documentation, and
lists the gaps.

## Summaries

**LightGBM fraud classifier.** A supervised model on 9 behavioural features of one outgoing payment. It is
trained on synthetic data with a time-based split. Its top-3 SHAP contributions become the MODEL reasons
that analysts see. It is never served alone: the hybrid uses it, and it is promoted through the MLflow
`champion` alias only after a measured gate passes.

**Isolation Forest anomaly model.** An unsupervised model fitted on normal training debits (4 features). Its
precision is low on its own, so it is a second signal for the hybrid and the source of the ANOMALY reason
("velocity_10m is 6.00, usually about 0.00").

**Hybrid fraud combiner.** This is what the API serves for transfers and bill payments when
`FRAUD_SCORER=hybrid`. It calibrates the LightGBM probability and stacks it with the anomaly score and rule
points. The baseline rules are a floor: the model can raise a decision, never lower it. If the model fails
or is too slow, the rules decide alone. REVIEW holds go to a fraud analyst with MFA (ADR-0019).

**Balance forecast.** AutoARIMA (naive methods for short histories), fitted per request on one owned
account's ledger history. It returns 80% and 95% intervals and the month-end balance after known
obligations, always labelled "Predicted, not exact". It was chosen by a rolling-origin backtest.

**K-Means segmentation.** An offline job assigns each customer one of five behavioural personas from five
ledger features. It does not run on the request path. It has no version string and no quality gate,
which the card states as a limitation.

**Product recommendation score (`rules-v1`).** Not a trained model. Eligibility rules come first, then a
hand-weighted formula orders the eligible products, with reasons in the customer's own INR figures. The
only demographic input is a consented age band. It is an eligibility reason only, never a ranking input
(ADR-0015).
