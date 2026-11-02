# Model card: rule-based product recommendation score

| | |
|---|---|
| Code | `ml/src/pbip_ml/recommendations/engine.py` (`relevance_score`, eligibility rules, reasons), `ml/src/pbip_ml/recommendations/contract.py` |
| `model_version` | `rules-v1` (`MODEL_VERSION` in `engine.py`), carried on every recommendation |
| Owner | `rachitst` (`.github/CODEOWNERS`, rule `*`) |
| Tasks | TASK-054; ADR-0015 |

## What it is

**Not a trained model.** Eligibility rules first exclude products the customer does not qualify for
(buffer, income, debt ratio, balance, health score). Eligible products are then ordered by a fixed,
hand-weighted linear formula per product type, squashed into (0, 1). The score is **not a probability**: it
only orders products for one customer (`engine.py` module docstring and `relevance_score`). No data exists
to train a propensity model (no recorded product take-ups).

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

**No evaluation report on main.** There are no outcome labels to evaluate a ranking against.

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
