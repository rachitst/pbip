# Model cards and governance pack (TASK-088)

One card per model that exists on `main`. Each model was checked in code before its card was written.
Every metric is copied from an existing report and cites the file and section it came from. Where no
evaluation report exists, the card says so instead of giving a number.

| Card | Code | `model_version` | Served? | Evaluation on main |
|---|---|---|---|---|
| [LightGBM fraud classifier](lightgbm-fraud.md) | `ml/src/pbip_ml/fraud/training.py` | part of `hybrid-lgbm-iforest-3` | through the hybrid (`FRAUD_SCORER=hybrid`) | `docs/ml/model-evaluation-report-v1.md` §3.2, §8, §9 |
| [Isolation Forest anomaly model](isolation-forest.md) | `ml/src/pbip_ml/fraud/training.py` | part of `hybrid-lgbm-iforest-3` | through the hybrid | `docs/ml/model-evaluation-report-v1.md` §3.2; `docs/ml/fraud-models.md` |
| [Hybrid fraud combiner](hybrid-fraud-combiner.md) | `ml/src/pbip_ml/fraud/hybrid.py` | `hybrid-lgbm-iforest-3` (`+rules-only` on fallback; the demo stack still serves `-1`) | yes: transfers and bill payments | `docs/ml/model-evaluation-report-v1.md` §3.1, §8, §9, §10 |
| [Balance forecast](balance-forecast.md) | `ml/src/pbip_ml/forecasting/model.py` | `forecast-arima-1` | yes: `GET /api/v1/analytics/forecast` | `docs/ml/balance-forecast.md` |
| [K-Means customer segmentation](kmeans-segmentation.md) | `ml/src/pbip_ml/segmentation/pipeline.py` | none defined in code | stored by an offline job, read by `GET /api/v1/analytics/segment` | no evaluation report on main |
| [Rule-based product recommendation score](recommendation-score.md) | `ml/src/pbip_ml/recommendations/engine.py` | `rules-v1` | yes | no ranking evaluation (no outcome labels); latency and the no-model decision: [`docs/ml/recommendation-propensity-decision.md`](../recommendation-propensity-decision.md) |
| [Simulated-uptake propensity model](recommendation-propensity.md) | `ml/src/pbip_ml/recommendations/training.py`, `propensity.py` | `sim-propensity-v1` | no: failed its gate against `rules-v1` (ADR-0049) | on SIMULATED uptake only: the card, from `models/recommendations/report.json` |

Not covered by a card:

- The baseline fraud rules (`apps/api/src/pbip_api/modules/fraud/baseline.py`, `baseline-rules-0.6`) are a
  hand-written rule set, not a model. They appear in the hybrid card as the floor and the fail-safe.
- The circular-transfer analysis (`docs/ml/circular-transfer-spike.md`, TASK-072) is a spike, not a served model.

## Governance in one page

The full mapping to RBI FREE-AI, the EU AI Act and SR 11-7 / PRA SS1/23, with evidence and gaps, is in
[governance-alignment.md](governance-alignment.md).

- **No black boxes (AGENTS.md section 8).** Fraud decisions return `risk_score`, `decision`, `reasons[]`
  (RULE / MODEL / ANOMALY) and `model_version`. The feature snapshot is stored in `fraud_events`.
  Customers never see reasons or scores.
- **Training is offline.** The API imports only `pbip_ml.*.contract` and inference modules (ADR-0007,
  ADR-0012, ADR-0019).
- **Data is synthetic and seeded** (`pbip_datagen`, TASK-023, ADR-0009). Fraud: seed 42 (3,000 customers,
  6 months) for training, seed 4242 (1,500 customers) as the held-out variant. Forecast backtest: seed 42.
  K-Means: `random_state=42`. All numbers measure how well the models learn planted patterns, not
  real-world performance.
- **Promotion.** Only the fraud models have a registry: MLflow registered model `pbip-fraud` with the
  aliases `challenger` and `champion`, moved by a measured metric gate (`ml/src/pbip_ml/fraud/promotion.py`,
  TASK-038; `docs/ml/fraud-models.md` "Model registry and promotion"). The other models have no registry
  entry; they change only through a reviewed code change, which should bump their `MODEL_VERSION` constant.
- **Fairness.** No model takes gender, religion, caste, ethnicity, marital status, health, precise
  location or declared income (ADR-0015 decision 3). The only demographic field anywhere is the
  recommendation engine's consented `age_band`, which is an eligibility reason only and never a ranking
  input (ADR-0015 decision 2). Each card lists the features, as verified in code.
- **Owner.** `.github/CODEOWNERS` has a single rule (`*`) with one owner, `rachitst`. Every card lists
  that owner, and every change to `ml/` needs that owner's review.
