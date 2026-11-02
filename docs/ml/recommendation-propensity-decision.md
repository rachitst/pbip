# Decision: no trained propensity model for product recommendations yet (TASK-054)

| | |
|---|---|
| Status | Decided: keep `rules-v1`; do not train a propensity model on synthetic data |
| Task / issue | TASK-054, issue #77 |
| Code | `ml/src/pbip_ml/recommendations/engine.py`, `apps/api/src/pbip_api/modules/recommendations/` |
| Model card | [`model-cards/recommendation-score.md`](model-cards/recommendation-score.md) |
| Date | 2026-11-03 |

## The rule we applied

Train a model only if honest labels can be generated and documented, with a time-based split, real
metrics and an MLflow run. Otherwise keep the rule-based score and say why (AGENTS.md section 8: no black
boxes, never claim more than the data supports).

An honest propensity label is an **observed adoption event after a feature cut-off date**: "the customer
opened or booked product X within N days after date T", where the event was produced by behaviour that is
independent of the scoring rules the model would replace. Features are computed from data up to T only.

## Why there is no honest label today

### 1. The synthetic generator simulates no product adoption

`data-gen` (`pbip_datagen.generator`, v1.0.0, and `pbip_datagen.fraud`, v2.2.0) simulates, per customer:
income on a calendar (salary, pension, allowance, freelance, business), an optional existing EMI fixed at
creation, utility bills, discretionary card/UPI spending with festive seasonality, `joined_on`,
`churned_on`, and injected fraud scenarios F1-F8. It never simulates a customer **taking up a product**:
no second account opened, no fixed deposit booked, no loan or credit line drawn, no card issued, no SIP
started, no insurance bought. The EMI is a static attribute drawn at customer creation, not an event that
follows a behavioural state. So there is nothing to put after a cut-off date T.

### 2. Adding adoption to the generator would produce made-up labels

We could add an adoption step to `data-gen` (for example "each month, a customer books an FD with
probability `sigmoid(a + b * buffer_months + ...)`"). We decided against it:

- **The label would be our own formula.** No real take-up data, survey or published response rate exists
  in this project to set `a`, `b`, ... The coefficients would be invented, and a logistic regression or
  GBM trained on the result would recover the invented coefficients. Its precision, recall and PR-AUC
  would measure how well the model inverts our generator, not how customers behave.
- **It cannot be independent of `rules-v1`.** Both would be expert guesses about the same thing (people
  with a cash buffer book deposits). Either the simulated choice reuses the ideas in `relevance_score`
  (circular: the model learns the rules back and "beats" the baseline only by noise), or it uses other
  ideas that are no better grounded (the "better" model is then better only at predicting a different
  guess). Neither result is evidence that customers would be served better.
- **The fraud model is not a precedent here.** Fraud labels are also synthetic, but each one marks an event
  the generator injected as a documented fraud typology (F1-F8), and the model's job is to detect those
  typologies. A propensity model's job is to predict a human choice; a generated choice function has no
  typology behind it.

### 3. Training and serving would see different features

Serving builds `CustomerRecommendationContext` from the operational ledger through the analytics module
(health score, cash-flow profile, segment) and the accounts module (balances). The synthetic dataset lives
in the separate `synthetic` schema (ADR-0009) and the analytics derivations live in `pbip_api`, which
`pbip_ml` may not import. A trained model would need those derivations re-implemented offline at each
cut-off date, which is a new feature pipeline with its own skew risk. That is worth building once real
outcomes exist, not to fit invented ones.

## What `rules-v1` meets today

| Acceptance criterion (TASK-054) | Status | Evidence |
|---|---|---|
| Eligibility rules first | Met | `evaluate_eligibility` runs before ranking in `generate_recommendations`; tests `test_eligibility_exclusion_*` in `ml/tests/test_recommendations.py` |
| Scores propensity with logistic regression / gradient boosting | **Not met** | This document. The score is a fixed, hand-weighted relevance formula and is labelled "not a probability" in the contract and API schema |
| Top 3 with human-readable reasons | Met | `top_k=3` default; every `Recommendation` has `reasons` (min 1) built from the customer's own figures in INR; integration tests in `apps/api/tests/integration/test_recommendations.py` |
| `GET /api/v1/recommendations` < 40 ms | Engine: met. Endpoint: **not demonstrated** | See below |

### Latency, measured 2026-11-03

Setup: in-process FastAPI `TestClient` against PostgreSQL 16 (Docker), one customer with 61 ledger
entries, 5 warm-up calls, then 3 rounds of 60 calls. Machine: 2-CPU laptop shared with other jobs,
1-minute load average 6.6-7.1 during the run (the CPUs were oversubscribed about 3x).

| What | p50 | p95 |
|---|---|---|
| Engine only (`generate_recommendations`, CPU time) | < 0.1 ms | 0.13-0.17 ms |
| Service (`get_customer_recommendations`, wall clock, includes the database) | 66-78 ms | 91-123 ms |
| HTTP through `TestClient` (wall clock) | 91-105 ms | 143-173 ms |
| HTTP, CPU time of the Python process only | 27-28 ms | 29-31 ms |

- The ranking itself is negligible: about 0.4% of the 40 ms budget. A unit test now guards it
  (`test_engine_p95_cpu_time_is_a_small_fraction_of_the_40ms_endpoint_budget`, p95 of `process_time` over
  540 calls must stay under 4 ms).
- The endpoint cost is the database work: **13 SQL statements per request** (authentication and customer
  lookup, accounts, health-score metrics, cash-flow profile, segment, consent and date of birth). On
  this oversubscribed machine the endpoint does **not** meet 40 ms, and the Python CPU time alone (about
  28 ms, which includes the test client's own work) leaves little headroom. Whether it meets 40 ms on an
  idle server has not been measured here; TASK-102 (k6 p95 on the compose stack) is the right place to
  measure it. If it does not, the fix is in the context build (fewer queries, or reuse of the stored
  monthly analytics), not in the engine, and is a separate change, tracked in issue #343.

A trained model would not change this picture: an LR or small GBM scores six products in well under a
millisecond. The latency question is independent of the model decision.

## What would be needed to train a propensity model later

1. **Exposure log.** Record which products were shown to which customer, when, at what rank and under
   which `model_version`. Without impressions, a click or a booking cannot be attributed to a
   recommendation, and the training data would only reflect what `rules-v1` chose to show (selection bias).
2. **Interest signal.** The clickstream `product_clicked` event with the catalogue `product_id` (TASK-055
   ingestion is on main; the web instrumentation is TASK-112, PR #333, open at the time of writing).
3. **Conversion signal.** The real take-up events now exist in the product: fixed-deposit booking
   (TASK-064), loan disbursal (TASK-047). A label is then "booked or opened product X within N days of
   being shown it", joined by customer and product, observed after the exposure.
4. **Point-in-time features.** Snapshot the `CustomerRecommendationContext` at exposure time (as
   `fraud_events` stores the fraud feature snapshot), so training uses exactly what serving saw and never
   data from after T.
5. **Enough outcomes.** Months of real traffic, so that a time-based split (train on earlier exposure
   months, test on later ones) has enough positives per product to report precision, recall, F1, PR-AUC
   and the confusion matrix against `rules-v1` on the same split.
6. **The usual path.** Train offline, track the run in MLflow, promote through the registry alias, export
   checksummed artefacts loaded at startup (the ADR-0019 pattern), keep eligibility rules first and keep
   reason strings. Changing the recommendation `contract.py` (for example to call the score a probability)
   would need an ADR.

Until then, the honest statement stays the one in the code: `rules-v1` orders eligible products by a
reviewed, hand-weighted formula; its score is not a probability and its ordering has not been validated
against customer behaviour.
