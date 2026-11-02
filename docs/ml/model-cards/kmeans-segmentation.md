# Model card: K-Means customer segmentation

| | |
|---|---|
| Code | `ml/src/pbip_ml/segmentation/pipeline.py` (`cluster_customers`), `apps/api/src/pbip_api/modules/analytics/segmentation_job.py` (offline job), `scripts/run_segmentation.py` |
| `model_version` | **none defined in code.** The pipeline and `customer_segments` rows carry no version string; a run is identified only by when it ran |
| Served | stored in `customer_segments` by the offline job; read by `GET /api/v1/analytics/segment` (own row, 404 until the job has run) |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-044; ADR-0012 |

## Intended use

Assign each customer one of five behavioural personas (HIGH_NET_WORTH, INACTIVE_SAVER, FREQUENT_TRANSACTOR,
DISCRETIONARY_SPENDER, BUDGET_CONSCIOUS), with a confidence and an explanation relative to the population
median. Used for analytics and as one input of the [recommendation score](recommendation-score.md)
(`target_segments`).

**Out of scope:** credit, pricing, fraud or eligibility decisions; any use as a demographic proxy;
real-time assignment on the request path (ADR-0012: training stays out of the request path).

## Training data

The bank's own ledger, not a synthetic export: per customer, POSTED non-reversal entries of all accounts
(ADR-0012 "Decision"). In local and demo environments that ledger is synthetic seed data. A customer
without ledger entries gets no segment; nothing is invented. Seed: `KMeans(n_clusters=5,
random_state=42, n_init=10)` on standardised features (`pipeline.py`). The job skips cleanly with fewer
than two eligible customers; exactly five clusters are always fitted.

## Features (5, verified in `pipeline.py` `FEATURE_NAMES`)

`recency_days`, `frequency`, `monetary_minor`, `savings_ratio`, `discretionary_ratio`. No demographic
attribute.

## Evaluation

**No evaluation report on main.** The pipeline computes Silhouette and Davies-Bouldin on every run
(`ClusteringValidationReport`), but no run's values are recorded in `docs/`. ADR-0012 states quality is
reported, not gated.

## Limitations

- Persona names come from a fixed ranking of centroids (highest monetary first, then recency, frequency,
  discretionary share, `_label_cluster_centroids`), so a cluster's name may fit its members loosely.
- No quality gate: a poor Silhouette score does not stop the job (ADR-0012 "Consequences").
- No version string, so a stored segment cannot be tied to a specific fit beyond its timestamp.
- Behaviour in the demo data is synthetic; personas on real customers will differ.

## Fairness

Five ledger-derived behavioural features only; no age, gender or other protected attribute (ADR-0015
decision 3). Monetary features correlate with income, so the HIGH_NET_WORTH persona reflects wealth by
construction; it must not be used to restrict access to products. No disparate-impact audit on main.

## Retraining and promotion

No registry or promotion. Re-running `scripts/run_segmentation.py` refits and upserts every customer's row in
one commit (ADR-0012). A method change is a reviewed code change; adding a `MODEL_VERSION` to the pipeline
and the table would need a migration and is not done.
