# ADR 0053: Uptake simulation v2 in data-gen, a ranking gate, and serving the promoted model
- Status: Accepted (owner's direction of 2026-11-04 for TASK-054 criterion 2; standing approval conditions below)
- Date: 2026-11-04
- Deciders: Architecture Team (TASK-054 criterion 2, issue #77)
- Builds on: [ADR-0049](0049-simulated-uptake-propensity-model-behind-the-rules.md) (simulated-uptake model behind
  the rules), [ADR-0015](0015-demographics-for-recommendations.md) (no age in ranking),
  [ADR-0030](0030-held-products-recommendation-input.md), [ADR-0041](0041-rm-list-propensity-order.md)

## Context
ADR-0049's model (`sim-propensity-v1`) missed its gate (macro PR-AUC +0.014 against +0.02), so `rules-v1` stayed.
That simulation lived in `pbip_ml` and let customers take up only what the eligibility rules offered, so a rule
output shaped the labels. The owner directed one final attempt: rebuild the uptake simulation in data-gen, driven
by customer behaviour plus noise (income stability, savings rate, spending mix, life-stage age band, holdings,
TASK-112 clickstream interest) and **never** by rules-v1's scoring or rules; train a LightGBM `lambdarank` ranker
and a logistic-regression baseline with a time-based split, tracked in MLflow; serve a model only if it beats
rules-v1 on NDCG@3 and hit-rate@3 on the held-out period, keeps the eligibility rules first, keeps readable
reasons and stays within the 40 ms endpoint budget; otherwise record the evidence and keep rules-v1.

## Decision
1. **Simulation in data-gen, independent of the rules.** `pbip_datagen.uptake` (`uptake-sim-2`) generates a
   seeded monthly panel (what the bank's data would hold, plus simulated `product_clicked` counts and uptakes);
   `python -m pbip_datagen uptake` exports it as CSV. Every coefficient and noise term, and why it is plausible,
   is in the [model card](../ml/model-cards/recommendation-propensity.md). A new import-linter contract forbids
   `pbip_datagen` from importing `pbip_ml`, and two tests check the generator's imports and that exporting loads
   no `pbip_ml` module. `pbip_ml.recommendations.simulation` (uptake-sim-1) and its `RULES_FOLLOW` scenario are
   removed.
2. **Eligibility first, outside the labels.** `pbip_ml.recommendations.panel` derives the serving context at each
   month from months `<= t`, keeps only the products `evaluate_eligibility` and `held_product_rule` allow as
   candidates, and labels them with the next month's simulated uptake.
3. **Two models, one feature function.** Both use `propensity.feature_vector(ctx)` (no clicks, no age band, no
   customer id): `sim-propensity-v2`, one standardised logistic regression per product (the existing JSON form),
   and `sim-lgbm-ranker-v2`, one LightGBM `lambdarank` model grouped by customer-month with the product as a
   categorical input (parameters fixed in code; early stopping on validation). Split: train on feature months
   2-11, validate on 13-14, test on 16-22 (one-month gaps, so no label crosses into a later block). One MLflow
   run per model in `pbip-recommendations`.
4. **The gate, fixed before the first run.** The candidate is the model with the higher NDCG@3 on the
   validation months. On the test months it must (a) beat rules-v1 on NDCG@3 and (b) on hit-rate@3, each with
   the 95% customer-level bootstrap interval of the difference above zero, (c) show no product the rules exclude
   when run through the real engine, (d) keep rules-v1's reasons exactly, (e) add at most 2 ms p95 of inference
   (5% of the 40 ms budget, #343). After the first run one check was added that did not change its verdict:
   (f) the candidate must have a served form; only the logistic regression has one, so a winning LightGBM
   ranker would need its own reviewed artefact format first.
5. **Result: promoted** (seed 42, 3,000 simulated customers x 24 months; full tables in the model card). The
   candidate is the logistic regression (validation NDCG@3 0.721 vs 0.709). On the test months: NDCG@3 0.721 vs
   rules-v1 0.499 (+0.222, interval +0.200 to +0.244), hit-rate@3 0.873 vs 0.741 (+0.132, +0.106 to +0.155),
   0 ineligible products and 0 changed reasons over 2,000 contexts, 0.035 ms added p95 (2-CPU laptop, load 3.7-3.8).
   The LightGBM ranker scored 0.707 / 0.868 and 5.9 ms p95 (`Booster.predict` per call).
6. **Serving.** No new code path: the API's loader (ADR-0049, `recommendations/scorer.py`) now expects
   `sim-propensity-v2` (a v1 artefact is refused). `make recommendation-model` produces the artefact; nothing is
   committed (`models/` and `data/recommendations/` are git-ignored, as for `models/fraud`). docker-compose sets
   `RECOMMENDATION_MODEL_DIR=/models/recommendations` and mounts `./models/recommendations` read-only;
   `.env.example` sets `RECOMMENDATION_MODEL_DIR=models/recommendations` for a local API. A missing directory,
   missing file, altered file or a run that failed the gate logs `recommendation_model_not_loaded` and serves
   rules-v1. The RM views stay on rules-v1 (ADR-0049 item 8). The engine's `propensity` parameter is typed by a
   small `OrderingModel` protocol so the offline checks can pass the ranker through the same engine path; the
   behaviour is unchanged.
7. **Labels.** Responses ordered by the model keep `score_from_simulated_data: true` and `score_version`
   `sim-propensity-v2 (trained on simulated uptake, not real customer behaviour)`; the schema descriptions name
   v2 (`make openapi`).

## What the result does and does not show
The labels are our formula. The model beats rules-v1 because it recovers this simulation's assumptions better
than rules-v1's hand weights do; a diagnostic "popularity" ranker (the product's training uptake rate) already
gains +0.100 NDCG@3 and +0.123 hit-rate@3, so much of rules-v1's deficit is not knowing which products the
simulation makes popular. The model's own signal shows at the top of the list (precision@1 0.527 vs 0.221 for
popularity). Base rates are inflated, the effects are invented, clicks and age drive labels the rankers cannot
see, and 27% of simulated uptakes are of products the rules would not offer. None of this is evidence about real
customers; the decision record lists what real data would need.

## Alternatives considered
- Keep rules-v1 regardless: rejected; the owner set a pass/fail rule in advance and the model passed it.
- Serve the LightGBM ranker: it was not the validation winner, its per-request `Booster.predict` tail (5.9 ms
  p95) would fail the latency check, and it would need a new artefact format and loader.
- Add click counts to the model: the served context has none, so this would change the context build (#343,
  another workstream) and the contract; left for when real clickstream data exists.

## Consequences
- Customer-visible: where `models/recommendations` holds a promoted model (local compose and demo after
  `make recommendation-model`), the customer's recommendation order changes and the card list shows the
  existing simulated-data note. Same products, same reasons. Without the model, nothing changes.
- No migration, no new endpoint, no new dependency (LightGBM, scikit-learn and MLflow are existing ML
  dependencies; the recommendation serving path imports none of them), no outbound data.
- Threat model T63 updated: the directory is now mounted read-only by docker-compose.

## Standing-approval conditions
(1) No change to authentication, RBAC, BOLA or audit; the route still takes the customer from the token and the
model can only re-order products the rules allow. (2) No money movement. (3) No migration. (4) No new service,
dependency or outbound data; the artefact is local JSON. (5) No fake data in any endpoint: every response the
model orders says it was trained on simulated uptake, and every metric is labelled simulated. (6) Within
TASK-054 criterion 2.
