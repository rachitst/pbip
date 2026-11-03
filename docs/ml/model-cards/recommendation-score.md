# Model card: rule-based product recommendation score

| | |
|---|---|
| Code | `ml/src/pbip_ml/recommendations/engine.py` (`relevance_score`, eligibility rules, reasons), `ml/src/pbip_ml/recommendations/contract.py` |
| `model_version` | `rules-v1` (`MODEL_VERSION` in `engine.py`), carried on every recommendation |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-054; ADR-0015 |
| Decision record | [No trained propensity model yet](../recommendation-propensity-decision.md) (2026-11-03) |
| Simulated-uptake model | [`sim-propensity-v1`](recommendation-propensity.md): trained and compared with this score on simulated uptake; not served (ADR-0049) |

## What it is

**Not a trained model.** Eligibility rules first exclude products the customer does not qualify for
(buffer, income, debt ratio, balance, health score). Eligible products are then ordered by a fixed,
hand-weighted linear formula per product type, squashed into (0, 1). The score is **not a probability**: it
only orders products for one customer (`engine.py` module docstring and `relevance_score`). No data exists
to train a propensity model (no recorded product take-ups): the synthetic generator simulates no product
adoption, and inventing an adoption process would only produce labels the model learns back. The full
reasoning, and what is needed to train one later, is in
[`recommendation-propensity-decision.md`](../recommendation-propensity-decision.md).

## Intended use

Order the eligible products in the catalogue for one customer, each with reasons built from the customer's
own figures in INR.

**Out of scope:** credit approval or pricing; comparing customers by score; any claim of take-up likelihood.

## Training data

None: the weights are hand-set constants in `relevance_score`. No seed applies.

## Inputs (verified in `contract.py` `CustomerRecommendationContext`)

`segment`, `health_score`, `monthly_inflow_minor`, `monthly_spend_minor`, `savings_ratio`,
`discretionary_ratio`, `buffer_months`, `emi_ratio`, `total_balance_minor`, `age_band`. All except
`age_band` are derived from the ledger. `relevance_score` uses savings ratio, discretionary ratio, buffer
months, monthly inflow, health score, segment match and EMI ratio; it does not read `age_band`.

## Evaluation

**No ranking evaluation.** There are no outcome labels to evaluate a ranking against, so no precision,
recall or PR-AUC can be reported, and none is claimed.

**Latency** (measured 2026-11-03, details in the decision record): the engine's p95 is 0.13-0.17 ms of CPU
time, guarded by `test_engine_p95_cpu_time_is_a_small_fraction_of_the_40ms_endpoint_budget`
(`ml/tests/test_recommendations.py`, bound 4 ms). The endpoint's time is the 13 SQL statements that build
the context; on an oversubscribed 2-CPU laptop its p95 was 143-173 ms, so the 40 ms target is not
demonstrated. It has not been measured on an idle server (TASK-102).

## Limitations

- Weights are expert judgement, not fitted; their ordering has not been validated against customer
  behaviour.
- The score is comparable only within one customer's list.
- Inputs inherit the limits of the segmentation (no version) and of the health-score and income
  derivations.

## Fairness (ADR-0015)

- The only demographic input is `age_band` (`18-25`, `26-35`, `36-50`, `51-59`, `60+`, or `unknown`),
  derived from an optional, self-declared date of birth, used only with explicit, withdrawable consent
  (ADR-0015 decisions 1 and 4). Until consented date of birth exists it is `unknown` (TASK-107).
- Age band is an eligibility reason only ("available because you are 60+"), never a ranking input
  (ADR-0015 decision 2; `engine.py` line `if ctx.age_band == "60+"`, with the comment stating the rule).
- Not collected or used: gender, marital status, religion, caste, ethnicity, health, precise location,
  family details, declared income or employer (ADR-0015 decision 3).
- Income-related inputs (`monthly_inflow_minor`) affect ranking of investment and loan products; that is a
  suitability signal from the ledger, not a protected attribute.

## Retraining and promotion

No training, registry or promotion. A weight or rule change is a reviewed code change in `engine.py` and must
bump `MODEL_VERSION` (`rules-v<n>`) so every recommendation shows which formula produced them.
