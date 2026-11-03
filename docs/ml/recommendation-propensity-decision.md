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

### Latency after the context-build reduction (#343), measured 2026-11-04

The context now costs 6 SQL statements (accounts, one pass over the customer's entries for both the
health window and the lifestyle features, segment, age band, loans, fixed deposits; the instalments
query only when there is a loan), and the HTTP request 7 (token check and customer, then the age
band reuses the loaded customer). Before: 10 and 12. Pinned by
`test_recommendation_context_is_six_queries_and_the_request_seven`; the output is unchanged
(equality tests in `test_recommendations.py`, and byte-identical responses in the benchmark below).

Setup: scratch PostgreSQL 16 (Docker), 21 customers with 12 months of history (1,160+ entries), the
measured customer with 253 entries; 20 warm-up calls, then 300 sequential calls per variant, main and
this change interleaved twice. Machine: 2-CPU Celeron N4500 (1.1 GHz) shared with other jobs, 1-minute
load average **7.5-8.7** throughout (about 4x oversubscribed); it could not be made quiet.

| What (p50 / p95, ms) | main (2 runs) | after (2 runs) |
|---|---|---|
| Real HTTP, separate uvicorn process, keep-alive client | 49.6-62.4 / 89.5-92.2 | 33.6-34.1 / 51.3-56.8 |
| Service call, wall clock | 33.5-44.7 / 53.9-63.8 | 26.2-30.4 / 39.0-39.3 |
| Service call, Python CPU | 16.3-16.6 / 17.7-18.2 | 10.5-10.8 / 11.5-11.7 |
| In-process `TestClient`, wall clock | 47.6-55.9 / 80.4-85.1 | 40.2-45.0 / 57.6-59.0 |
| uvicorn process CPU per request | 22.6 | 15.2 |

**Floor on this hardware.** A request is about 15 ms of server CPU (FastAPI, auth, SQLAlchemy, the
aggregation; the engine is < 0.2 ms) plus about 10 database round trips (7 statements, BEGIN,
ROLLBACK and the pool pre-ping). A `SELECT 1` round trip is 0.28 ms p50 here but 2.3 ms p95 under this
load, and the largest statement (the entries pass) executes in about 1-3 ms in PostgreSQL. On an idle
machine that adds up to roughly 20-25 ms, inside 40 ms; at 4x oversubscription the CPU part stretches
and the measured HTTP p95 is 51-57 ms. So the endpoint does **not** meet 40 ms p95 over HTTP on this
loaded laptop; the service-level p95 is at the limit (39 ms). An idle-machine run is still needed to
demonstrate the HTTP budget (`scripts/measure_analytics_latency.py` against a quiet stack).

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
