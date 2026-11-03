# ADR 0049: A propensity model trained on simulated uptake, behind the rules, served only if it beats rules-v1
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (TASK-054 criterion 2, issue #77; owner's direction of 2026-11-04)
- Builds on: [ADR-0015](0015-demographics-for-recommendations.md) (no age in ranking),
  [ADR-0030](0030-held-products-recommendation-input.md) (held products),
  [ADR-0033](0033-fraud-artifact-schema-and-allow-listed-unpickler.md) (safe artefact loading),
  [ADR-0041](0041-rm-list-propensity-order.md) (RM order by the rule score)

## Context
TASK-054 criterion 2 asks for a logistic-regression or gradient-boosting propensity score.
[`recommendation-propensity-decision.md`](../ml/recommendation-propensity-decision.md) (2026-11-03) kept the
hand-weighted `rules-v1` because PBIP has no recorded product take-ups, and warned that labels from an invented
adoption process measure only how well a model inverts the invention. The owner has since directed that a model be
trained on a **documented, clearly labelled simulation** of uptake, placed behind the existing eligibility rules,
compared with `rules-v1` on the same simulated data with a time-based split, and served only if it beats
`rules-v1`; otherwise `rules-v1` stays and the result is reported honestly.

## Decision
1. **Simulation, labelled as such.** `pbip_ml.recommendations.simulation` (`uptake-sim-1`) generates a seeded
   population and monthly `CustomerRecommendationContext` snapshots, and draws next-month uptake for each product the
   rules allow, from per-product logits whose every assumption (personas, base rates, drivers, unobserved traits,
   noise) is written in the [model card](../ml/model-cards/recommendation-propensity.md). It is offline code only.
2. **Model.** One standardised logistic regression per product (`sim-propensity-v1`) on `feature_vector(ctx)`, the
   same function at training and serving, so the features cannot diverge. No `age_band`, no `customer_id`.
3. **Behind the rules.** `generate_recommendations(..., propensity=model)` scores only the offers that
   `evaluate_eligibility` and `held_product_rule` already allow; it re-orders them and keeps every reason. It never
   adds or removes a product.
4. **Time-based split and metrics.** Train on feature months <= 14, test on >= 16 (labels are the following month,
   so no training label reaches the test period). Per product PR-AUC, Brier and ECE; per customer-month
   precision@k and recall@k (k = 1, 3); the same for `rules-v1` on the same rows. Two sensitivity populations:
   perturbed effects, and `RULES_FOLLOW` (uptake follows `rules-v1`'s own formula). The run is logged to MLflow
   (`pbip-recommendations`).
5. **Promotion gate, fixed before the first run.** Macro PR-AUC at least 0.02 above `rules-v1`; precision@1 and
   recall@3 not below it; no product's PR-AUC below it; every product's ECE <= 0.02. `train` writes
   `report.json` always, and `propensity_model.json` plus a SHA-256 `manifest.json` with `"promoted": true` only
   if the gate passes; otherwise it deletes them.
6. **Serving, fail safe.** `RECOMMENDATION_MODEL_DIR` (unset by default) names the directory. The API loads it once
   at start-up (`recommendations/scorer.py`), plain JSON, no pickle: the manifest must match, carry the gate
   verdict, the feature list and format must match the code, every number must be finite and the model must cover
   exactly the catalogue. Anything else logs `recommendation_model_not_loaded` and serves `rules-v1`. The loader
   is inference-only; a new import-linter contract keeps `pbip_api` and the serving modules off the simulation and
   training code.
7. **Labelling.** `GET /api/v1/recommendations` gains `score_version` (a human-readable label, e.g.
   `sim-propensity-v1 (trained on simulated uptake, not real customer behaviour)`) and
   `score_from_simulated_data`; each item's `model_version` names the ordering. The customer card list shows a fixed
   note when the flag is true, and the assistant's investment reply names the model as trained on simulated uptake.
   The `score` description in `contract.py` and the schema now covers both meanings.
8. **The RM views stay on `rules-v1`** (`insights.service._ranked_products` passes `propensity=None`): the RM
   order, the loan filter thresholds and the "rule-based" labels are defined on the rule score (ADR-0041), and a
   probability fitted to simulated uptake must not rank real customers against each other.

## Result of the first run (seed 42, 3,000 customers, 24 months)
The model is **not promoted**. It beats `rules-v1` on precision@1 (0.614 vs 0.468) and on five of six products'
PR-AUC, and is calibrated, but its macro PR-AUC gain is +0.014 (gate 0.02) and its SIP PR-AUC is 0.043 against
0.046. The gate was not changed after seeing this, and the simulation was not re-tuned. On `RULES_FOLLOW`,
`rules-v1` wins clearly (macro PR-AUC 0.225 vs 0.117), which shows the comparison mostly measures which ranker
matches the simulation's assumptions. Full numbers: the model card.

## Consequences
- Customer-visible behaviour is unchanged: the API serves `rules-v1` (no model directory is configured anywhere,
  and the trained model would not load). The response gains two additive fields (`make openapi`).
- To serve a future version that passes the gate: run `make recommendation-model`, set
  `RECOMMENDATION_MODEL_DIR` to `models/recommendations` and mount it read-only into the API (as the fraud models
  are). Any change to the simulation, the features or the gate is a reviewed change that bumps the versions.
- TASK-054 criterion 2 now has a trained, evaluated model with honest metrics, but no served model; whether that
  closes the criterion is the owner's call.
- No new dependency (scikit-learn, numpy and MLflow are already ML dependencies; the API side uses none of them),
  no migration, no new endpoint, no outbound data.
- Threat model T62: a tampered model directory could re-order (never widen) the eligible products.

## Standing-approval conditions
(1) No change to authentication, RBAC, BOLA or audit; the route still takes the customer from the token, and the
model can only re-order products the rules allow. (2) No money movement. (3) No migration. (4) No new service,
dependency or outbound data; the artefact is local JSON. (5) No fake data in any endpoint: the simulation is
labelled as simulated everywhere it appears, and the metrics are real measurements on it, reported with the gate
failure. (6) Within TASK-054 criterion 2.
