# Model card: simulated-uptake propensity model (`sim-propensity-v1`)

| | |
|---|---|
| Code | `ml/src/pbip_ml/recommendations/simulation.py` (the simulation), `training.py` and `train.py` (offline), `propensity.py` (serving, JSON only) |
| `model_version` | `sim-propensity-v1`; label `sim-propensity-v1 (trained on simulated uptake, not real customer behaviour)` |
| Baseline | `rules-v1` ([card](recommendation-score.md)), on the same simulated rows |
| Decision | [ADR-0049](../../adr/0049-simulated-uptake-propensity-model-behind-the-rules.md) |
| Task | TASK-054 criterion 2 (issue #77) |
| Served? | **No.** The model did not pass its promotion gate (below), so `make recommendation-model` writes only `report.json` and the API keeps `rules-v1` |
| Date | 2026-11-04 |

> **Everything on this page is measured on SIMULATED product uptake.** PBIP has no recorded product
> take-ups. The uptake process below is invented, with every assumption written down. The numbers say how
> well each ranker recovers those assumptions. They are **not** evidence of how real customers behave and
> must never be presented as real-world performance.

## What it is

Six logistic regressions, one per catalogue product, each predicting "takes this product up next month"
from the same `CustomerRecommendationContext` the rules see (`propensity.feature_vector`, 23 inputs:
log income, log spend, savings ratio and its negative part, lifestyle share and lifestyle spend in rupees,
buffer months and its square, EMI ratio, health score, log balance, log fixed-deposit principal, five
segment flags, six held-product flags). `age_band` and `customer_id` are not inputs (ADR-0015).
Standardised inputs, L2 penalty `C=1.0`, no class weights (so the probabilities keep the simulated base
rates). The served form would be plain JSON (coefficients, means, scales) checked against a SHA-256
manifest; nothing is unpickled.

It sits **behind** the eligibility and held-product rules: it can only re-order the products those rules
already allow. It never adds a product, never removes one, and never changes a reason
(`test_the_model_never_offers_a_product_the_rules_exclude`,
`test_the_model_reorders_the_same_offers_with_the_same_reasons_and_its_own_label`).

## The simulation (`uptake-sim-1`)

Deterministic: each simulated customer has its own RNG stream (`"uptake-sim-1:<seed>:<index>"`), so a
customer's history does not depend on the population size or on how many months are simulated
(`test_the_simulation_is_deterministic_for_a_seed_and_changes_with_it`,
`test_a_customers_history_does_not_depend_on_the_population_size`). Training run: seed 42, 3,000
customers, 24 months from January 2025.

### Population (personas)

| Persona | Share | Median income (INR/month) | Spend / income | Lifestyle share | Starting buffer (months) | Holds a loan | Holds a savings account | Holds a deposit |
|---|---|---|---|---|---|---|---|---|
| Salaried | 45% | 55,000 | 0.60-0.95 | 0.15-0.40 | 0.5-4 | 40% | 80% | 8% |
| Gig worker | 20% | 30,000 | 0.80-1.10 | 0.20-0.45 | 0-1.5 | 25% | 55% | 2% |
| Affluent | 12% | 2,50,000 | 0.40-0.70 | 0.25-0.50 | 4-18 | 45% | 90% | 30% |
| Retiree | 13% | 35,000 | 0.60-0.90 | 0.05-0.20 | 3-15 | 5% | 85% | 35% |
| Student | 10% | 12,000 | 0.85-1.05 | 0.30-0.55 | 0-1 | 2% | 50% | 0% |

Income varies between customers (lognormal, sigma 0.4-0.6) and month to month (CV 3% for retirees to
35% for gig workers); salaried customers get 6% a year. Spending varies 8% a month and rises in October
and November with the lifestyle share. Nobody spends money they do not have. A loan is home 40%, auto
35%, personal 25%, with an EMI of 10-40% of income. 15% of customers are unsegmented (new). The context
averages the last three months, like the analytics module; the health score is a simplified
four-component approximation (savings rate, buffer, debt, income stability, 25 points each), not the
API's exact formula.

### Uptake assumptions (monthly logit, per product the rules allow that month)

`P(uptake next month) = 0.0005 + 0.9995 * sigmoid(base + behaviour + latent)`

| Product | Base logit | Behaviour (observable; the "why") | Unobserved |
|---|---|---|---|
| Savings account | -5.0 | `6.0 * clip(savings ratio, 0, 0.4) + 0.5 * log1p(balance / INR 1 lakh)`: money left over, nowhere separate to put it | responsiveness |
| Fixed deposit | -6.3 | `0.35 * min(buffer, 12) - 0.15 * max(buffer - 12, 0) + 1.0 if lifestyle share < 15% - 0.6 if already holds a deposit`: idle cash, but very large buffers are invested elsewhere | responsiveness, `-0.45 * risk appetite` |
| Personal credit line | -5.0 | `8.0 * clip(-savings ratio, 0, 0.3) + 1.2 if buffer < 1 month + 0.3 * log(income / 25,000)`: borrowing demand comes from a squeeze, not from comfort | responsiveness, `0.7 * credit appetite` |
| Rewards credit card | -4.8 | `1.3 * log(lifestyle spend in INR / 10,000) + 0.6 if FREQUENT_TRANSACTOR`: rewards pay off on high spend in rupees | responsiveness, `0.7 * credit appetite` |
| Index fund SIP | -6.4 | `5.0 * clip(savings ratio, 0, 0.5) + 0.12 * min(buffer, 12) + 0.4 * log(income / 50,000)`: a steady surplus and a cushion | responsiveness, `0.9 * risk appetite` |
| Family protection cover | -5.0 | `1.6 if holds a home loan + 0.9 if holds an auto loan + 2.0 * min(EMI ratio, 0.6) + 0.3 * log(income / 30,000)`: a secured loan to protect | responsiveness |

- **Noise.** Three per-customer traits no model input carries: responsiveness `N(0, 0.5)` added to every
  logit, risk appetite `N(0, 1)` and credit appetite `N(0, 1)`. Plus 0.05% a month of uptake unrelated to
  anything. So no ranker can be perfect.
- **Only what the rules allow.** A customer can take up only a product the eligibility and held-product
  rules offer that month: the bank sells nothing else
  (`test_the_simulation_only_lets_customers_take_up_what_the_rules_offered`).
- **Effects of uptake.** A savings account or a credit line (a personal loan of 1.5 months' income, repaid
  over 24 months) becomes held; a fixed deposit moves 30% of the balance into a held deposit. A card, SIP
  or cover has no held kind in PBIP (ADR-0030), so after one the customer leaves that product's rows.
- **Why these and not rules-v1's.** Each driver is a common-sense reason someone might take the product up,
  chosen to differ from rules-v1 where rules-v1 encodes "comfort" (for example, rules-v1 scores the credit
  line higher for higher income and debt room; this simulation makes demand come from a squeeze). The
  sizes are invented. This is the circularity the [decision record](../recommendation-propensity-decision.md)
  warned about: if the simulation is a different guess from the rules, a model fitted to it "beats" the
  rules by being better at that guess. The `RULES_FOLLOW` sensitivity scenario below shows the other side.

Resulting base rates on the test months (per product the rules allowed): savings account 2.3%, fixed
deposit 3.8%, credit line 1.0%, rewards card 1.7%, SIP 2.5%, family cover 1.3% a month.

## Split (no leakage)

A row is (customer, feature month, allowed product); its label is observed in the following month.
Train on feature months 2-14 (labels up to month 15), skip month 15, test on feature months 16-22 (labels
16-23). Every training label is earlier than the first test month
(`test_the_time_split_keeps_every_training_label_before_the_first_test_month`), and a month's context is
built from that month and earlier only (`test_features_at_a_month_use_no_later_month`: simulating more
months never changes an earlier month's context or label). Train: 101,304 rows, 2,601 uptakes. Test:
50,647 rows, 1,043 uptakes, 18,497 customer-months, 1,026 of them with at least one uptake.

## Evaluation on the simulated test months (seed 42, `report.json`)

Ranking, per customer-month with at least one uptake (ties keep catalogue order):

| | `sim-propensity-v1` | `rules-v1` |
|---|---|---|
| precision@1 | 0.614 | 0.468 |
| recall@1 | 0.607 | 0.462 |
| precision@3 | 0.318 | 0.313 |
| recall@3 | 0.939 | 0.924 |
| macro PR-AUC (6 products) | 0.048 | 0.035 |

recall@3 is close to 1 for both because most customers are allowed three products or fewer.

Per product (PR-AUC over every allowed row; the uptake rate is the PR-AUC of a random ranker):

| Product | Test rows | Uptakes | Uptake rate | PR-AUC model | PR-AUC rules-v1 | Model mean p | Brier | ECE |
|---|---|---|---|---|---|---|---|---|
| Savings account | 3,443 | 78 | 0.023 | 0.067 | 0.048 | 0.030 | 0.022 | 0.010 |
| Fixed deposit | 11,666 | 445 | 0.038 | 0.072 | 0.051 | 0.046 | 0.036 | 0.009 |
| Personal credit line | 11,459 | 115 | 0.010 | 0.020 | 0.012 | 0.014 | 0.010 | 0.004 |
| Rewards credit card | 5,229 | 91 | 0.017 | 0.047 | 0.031 | 0.029 | 0.017 | 0.012 |
| Index fund SIP | 5,281 | 132 | 0.025 | **0.043** | **0.046** | 0.032 | 0.024 | 0.010 |
| Family protection cover | 13,569 | 182 | 0.013 | 0.041 | 0.021 | 0.016 | 0.013 | 0.004 |

ECE: expected calibration error over 10 equal-count bins. rules-v1's score is not a probability, so no
calibration is reported for it. The model's mean probabilities run 0.1-1.2 points above the observed
test rates (the training months had more uptake: 2.6% of rows against 2.1%); the cause of the drift was
not investigated.

**Latency.** Scoring all six products is a few dozen multiplications in pure Python; the unit test bounds
the p95 at 2 ms of CPU (`test_scoring_six_products_costs_well_under_a_millisecond`).

### Promotion gate (fixed before the first run; ADR-0049)

All must hold: macro PR-AUC at least 0.02 above rules-v1; precision@1 and recall@3 not below rules-v1; no
product's PR-AUC below rules-v1's; every product's ECE at most 0.02.

| Check | Result |
|---|---|
| macro PR-AUC gain >= 0.02 | **fail** (+0.014) |
| precision@1 not worse | pass |
| recall@3 not worse | pass |
| PR-AUC not worse, each product | **fail for the index fund SIP** (0.043 vs 0.046); pass for the other five |
| ECE <= 0.02, each product | pass |

**Not promoted.** The model ranks better than rules-v1 on five of six products and at the top of the list,
on this simulation, but it does not clear the margin set in advance, and it ranks the SIP slightly worse.
The gate was not loosened after seeing the result, and the simulation was not re-tuned to pass it.

Supplementary, after the gate (not used to decide): a bootstrap over test customer-months (300
resamples, seed 0) puts the macro PR-AUC gain at +0.014 (95% interval +0.009 to +0.020), so the model
is better than rules-v1 on this simulation, though not by the margin set in advance. The SIP difference is
-0.002 (-0.020 to +0.006), and the credit-line difference is +0.007 (-0.001 to +0.018): neither is
distinguishable from zero at this sample size.
`rules-v1` stays the served ordering.

### Sensitivity (the same trained model, other simulated populations)

| Scenario (1,000 customers, test months) | macro PR-AUC model / rules-v1 | precision@1 model / rules-v1 |
|---|---|---|
| Perturbed: every effect x0.7, every base logit +0.3, seed 1042 | 0.050 / 0.036 | 0.542 / 0.401 |
| `RULES_FOLLOW`: uptake follows rules-v1's own relevance score, seed 1043 | 0.117 / **0.225** | 0.820 / **0.882** |

On `RULES_FOLLOW` rules-v1 wins clearly, as it should: whichever ranker matches the simulation's
assumptions wins. The comparison is only as meaningful as the assumptions.

## Intended use and limits

- Intended (if a later version passes the gate): re-order the eligible products for one customer, labelled
  as trained on simulated uptake in the API response (`score_version`, `score_from_simulated_data`), the
  customer's card list (a fixed note) and the assistant's reply.
- Not for: credit decisions, pricing, comparing customers (the RM list stays on rules-v1, ADR-0041), or
  any claim about real take-up.
- Synthetic evaluation: the labels are our own formula, so the metrics measure agreement with the formula.
  The population, base rates and effect sizes are invented. The simplified health score differs from the
  API's. No exposure log exists, so real data would also carry selection bias from what rules-v1 showed.

## Reproduce

`make recommendation-model` (`python -m pbip_ml.recommendations.train --out models/recommendations`):
a few seconds of CPU; writes `models/recommendations/report.json`, logs the run to MLflow experiment
`pbip-recommendations` (local SQLite fallback when the server is down). Model files and a manifest are
written only if the gate passes.

## Fairness (ADR-0015)

`age_band` is neither a simulation driver nor a model input; segment and held-product flags are
behavioural. Income enters through log income, as in rules-v1.
