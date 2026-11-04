# Model card: simulated-uptake propensity model (`sim-propensity-v2`)

| | |
|---|---|
| Code | `data-gen/src/pbip_datagen/uptake.py` (the simulation, `uptake-sim-2`), `ml/src/pbip_ml/recommendations/panel.py`, `training.py`, `train.py` (offline), `propensity.py` (serving, JSON only) |
| `model_version` | `sim-propensity-v2`; label `sim-propensity-v2 (trained on simulated uptake, not real customer behaviour)` |
| Compared with | `rules-v1` ([card](recommendation-score.md)) and a LightGBM `lambdarank` ranker (`sim-lgbm-ranker-v2`), on the same simulated rows |
| Decision | [ADR-0053](../../adr/0053-simulated-uptake-v2-ranker-gate-and-serving.md) (builds on [ADR-0049](../../adr/0049-simulated-uptake-propensity-model-behind-the-rules.md)) |
| Task | TASK-054 criterion 2 (issue #77) |
| Served? | **Yes, where `RECOMMENDATION_MODEL_DIR` holds a promoted model** (docker-compose mounts `./models/recommendations`). It passed its gate on the simulated test months. `make recommendation-model` produces it; nothing is committed |
| Date | 2026-11-04 |

> **Everything on this page is measured on SIMULATED product uptake.** PBIP has no recorded product
> take-ups. The uptake process below is invented, with every assumption written down. The numbers say how
> well each ranker recovers those assumptions. They are **not** evidence of how real customers behave and
> must never be presented as real-world performance.

## What it is

Six logistic regressions, one per catalogue product, each predicting "takes this product up next month"
from the `CustomerRecommendationContext` the rules see (`propensity.feature_vector`, 23 inputs: log
income, log spend, savings ratio and its negative part, lifestyle share and lifestyle spend in rupees,
buffer months and its square, EMI ratio, health score, log balance, log fixed-deposit principal, five
segment flags, six held-product flags). Standardised inputs, L2 penalty `C=1.0`, no class weights.
`age_band`, `customer_id` and clickstream counts are not inputs: the served context does not carry
clicks, and ADR-0015 keeps the age band out of ranking. The served form is plain JSON (coefficients,
means, scales) checked against a SHA-256 manifest; nothing is unpickled; scoring is pure Python.

It sits **behind** the eligibility and held-product rules: the engine builds the allowed offers first and the
model only re-orders them. It never adds a product, never removes one and never changes a reason.

## The simulation (`uptake-sim-2`, data-gen)

**Independence from rules-v1.** The labels are generated in `pbip_datagen.uptake`, which imports nothing
from `pbip_ml`: no rules-v1 score, no eligibility rule, no engine output can feed them. This is enforced by
an import-linter contract ("simulated uptake labels must not depend on the recommendation rules") and by
`test_the_label_generator_imports_nothing_from_the_recommendation_engine` and
`test_generating_and_exporting_the_panel_loads_no_pbip_ml_module` (data-gen tests). The previous
simulation (`uptake-sim-1`, ADR-0049) applied the eligibility rules inside the label generator and had a
`RULES_FOLLOW` scenario driven by rules-v1's score; both are gone. The eligibility rules are applied
**afterwards**, by `panel.py`, when it builds the candidate rows.

**Determinism.** Each customer has its own stream `default_rng([seed, index, 0x5E2])`; a customer's history
depends neither on the population size nor on the number of months (tested). No wall clock. Money in paise.
Training run: seed 42, 3,000 customers, 24 months from January.

### Population

The five data-gen personas (`personas.py`: salaried professional 40%, student 15%, freelancer 15%, business
owner 15%, retiree 15%) with their income ranges (log-uniform within the range), savings-rate ranges,
EMI probability and category weights.

| Assumption | Value | Why it is plausible |
|---|---|---|
| Life-stage age band per persona | student 18-25; salaried 15/45/35/5% over 18-25/26-35/36-50/51-59; freelancer 20/45/30/5%; business owner 20/50/30% over 26-35/36-50/51-59; retiree 10% 51-59, 90% 60+ | Follows the personas' life stages; retirees mostly past 60 |
| Income variation (CV a month) | salary 3%, pension 2%, allowance 10%, business 22%, freelance 30% | Salaries and pensions are fixed; self-employed income is lumpy |
| Thin months (income x 0.1-0.4) | freelance 12%, business 6%, allowance 5% of months | Gaps between contracts and slow trading months |
| Salary rise | 7% a year for salaried | Typical annual appraisal |
| Spending mix | persona category weights, Dirichlet-perturbed per customer (concentration 40); lifestyle share = dining + shopping + entertainment + travel, x1.25 in October-November | Same mix as the transaction generator; Diwali season spending |
| Living spending | income x (1 - savings target) - EMI, at least 30% of income, 8% monthly noise; never more than the cash available | People spend to a habit; no overdraft |
| Holdings at start | savings account 60% (else current account); a loan with the persona's EMI probability (home 40%, auto 35%, personal 25%, EMI 10-35% of income); a fixed deposit 30% of retirees, 8% of others (half the starting balance) | Most customers have a savings account; retirees hold deposits |
| Clickstream interest (TASK-112 `product_clicked`) | per customer and product, latent interest AR(1) with persistence 0.85, N(0, 1) stationary; clicks a month ~ Poisson(exp(-2.0 + 0.9 x interest)) | Interest drifts over months; a click is a noisy sign of it. About 0.17-0.19 clicks per product a month |

### Uptake (monthly logit, per product, from the state at the end of the previous month)

`P(uptake) = 0.0005 + 0.9995 x sigmoid(base + behaviour + life stage + 0.5 x interest + 0.5 x log(1 + clicks last month) + responsiveness + shock)`

| Product | Base logit | Behaviour (observable state) | Life stage (logit) | Why |
|---|---|---|---|---|
| Savings account | -4.6 | `4.0 x clip(savings rate, 0, 0.4) + 0.8 x income stability - 3.0 if it already holds one` | 18-25 +0.5 | Money left over and nowhere separate to put it; young customers open first accounts |
| Fixed deposit | -6.6 | `0.25 x min(buffer, 12) + 1.5 x income stability + 2.0 x clip(savings rate, 0, 0.4) - 1.2 x (lifestyle share - 0.3) + 0.3 if it holds a deposit` | 18-25 -0.5, 51-59 +0.5, 60+ +0.9 | Idle cash, a predictable income and frugal spending; deposits are a habit of older savers |
| Personal credit line | -5.4 | `6.0 x clip(-savings rate, 0, 0.3) + 1.0 if buffer < 1 month + 1.0 x (1 - income stability) - 1.0 if it holds a personal loan` | 26-35 +0.4, 60+ -0.8 | Borrowing demand comes from a squeeze, not comfort |
| Rewards credit card | -5.2 | `1.2 x log(lifestyle spend INR / 10,000) + 2.0 x (lifestyle share - 0.3) + 0.6 x income stability` | 18-25 +0.2, 26-35 +0.4, 60+ -0.7 | Rewards pay off on high lifestyle spending |
| Index fund SIP | -6.2 | `4.0 x clip(savings rate, 0, 0.5) + 0.10 x min(buffer, 12) + 1.0 x income stability` | 26-35 +0.6, 36-50 +0.3, 60+ -0.8 | A steady surplus to commit monthly; younger investors take market risk |
| Family protection cover | -5.6 | `1.4 if it holds a home loan + 0.7 if an auto loan + 0.8 x income stability` | 18-25 -0.8, 26-35 +0.4, 36-50 +0.7, 60+ -0.3 | A secured loan and dependants to protect |

Income stability is `1 - min(CV of the last 6 incomes / 0.4, 1)`; the savings rate and buffer use the last
three months, as the analytics module does.

- **Noise.** Per customer responsiveness `N(0, 0.5)` on every product; a monthly shock `N(0, 0.3)` per
  product; 0.05% a month of uptake unrelated to anything; plus the Bernoulli draw itself. Interest, clicks,
  life stage and the true income stability are unobserved by both rankers (the context only has a
  health score that partly reflects stability), so no ranker can be perfect.
- **Calibration.** Only the base logits were set by looking at data, and only at the overall monthly rates
  (no model had been trained): the fixed deposit's was moved from -6.0 to -6.6 to bring it from 5.3% to 3.7%.
  Resulting monthly rates: savings account 1.5%, fixed deposit 3.7%, credit line 1.4%, rewards card 2.9%,
  SIP 1.9%, family cover 1.3%. **Real cross-sell response rates are much lower**; these are inflated so the
  test period has enough positives.
- **Effects of uptake.** A savings account or credit line becomes held (the credit line draws 1.5 months of
  income, repaid over 24 months); a fixed deposit moves 30% of the balance into a held deposit; interest in a
  product falls by 1.5 after taking it. A card, SIP or cover has no held kind in PBIP (ADR-0030), so it is
  taken at most once.
- **Uptake is independent of eligibility.** The generator does not know the rules, so 27% of simulated
  uptakes (2,028 of 7,463 next-month uptakes) are of products the rules would not offer that month. Those
  are not candidate rows and are not evaluated; read them as the customer getting the product elsewhere.

## Split (no leakage)

A row is (customer-month `t`, product the eligibility and held-product rules allow); the context uses months
`<= t` only (`test_the_context_at_a_month_uses_no_later_month`); the label is the uptake in month `t + 1`.
Train on feature months 2-11 (102,335 rows, 2,812 uptakes); validate on 13-14 (model choice, LightGBM early
stopping: 27 rounds); test on 16-22 (71,329 rows, 1,654 uptakes, 18,681 customer-months, 1,586 with an
uptake). Each block's last label is before the next block's first feature month
(`test_the_time_split_keeps_every_label_before_the_next_block`).

## Evaluation on the simulated test months (seed 42, `report.json`)

Per customer-month with at least one uptake among its candidates; binary relevance; ties keep catalogue
order. 90% of those customer-months have more than three candidates, so hit-rate@3 is not trivially 1.

| Ranker | NDCG@3 | hit-rate@3 | precision@1 | MRR | p95 scoring per request |
|---|---|---|---|---|---|
| `rules-v1` | 0.499 | 0.741 | 0.190 | 0.483 | 0.041 ms |
| **`sim-propensity-v2`** (LR) | **0.721** | **0.873** | **0.527** | **0.705** | 0.076 ms |
| `sim-lgbm-ranker-v2` (LightGBM lambdarank) | 0.707 | 0.868 | 0.505 | 0.690 | 5.9 ms |
| popularity (diagnostic) | 0.599 | 0.864 | 0.221 | 0.543 | - |

Difference from `rules-v1`, with 95% intervals from 1,000 bootstrap resamples of **customers** (seed 0):

| Ranker | NDCG@3 | hit-rate@3 |
|---|---|---|
| `sim-propensity-v2` | +0.222 (+0.200 to +0.244) | +0.132 (+0.106 to +0.155) |
| `sim-lgbm-ranker-v2` | +0.208 (+0.188 to +0.230) | +0.126 (+0.103 to +0.150) |
| popularity (diagnostic) | +0.100 (+0.082 to +0.119) | +0.123 (+0.100 to +0.147) |

Latency: wall clock to score one request's candidates, 2,000 test contexts, on a 2-CPU Celeron N4500 laptop,
measured with the 1-minute load average below 4 (3.72 before, 3.82 after). The LR adds 0.035 ms at p95 over
rules-v1's own scoring. (The training run's own measurement, at load 4.98, gave 0.038 ms added; the gate passes
either way.) The LightGBM figure is `Booster.predict` per request; its tail is the library's per-call
overhead under load.

**Popularity**, added as a diagnostic after the first run (it is not part of the gate and did not change the
verdict), ranks every customer's candidates by the product's training uptake rate. It shows that much of
rules-v1's deficit is that its formula does not know which products are taken up more often **in this
simulation**: popularity alone matches the model's hit-rate@3 gain. The model's further gain is in the order
of the top: NDCG@3 0.721 vs 0.599 and precision@1 0.527 vs 0.221, which is customer-specific signal.

### Promotion gate (fixed before the first run; ADR-0053)

The candidate is the model with the higher **validation** NDCG@3 (LR 0.721, LightGBM 0.709: LR). It is
served only if all of these hold on the **test** months:

| Check | Result |
|---|---|
| NDCG@3 above rules-v1, and the 95% customer-bootstrap interval of the difference above 0 | pass (+0.222, +0.200 to +0.244) |
| hit-rate@3 above rules-v1, same interval rule | pass (+0.132, +0.106 to +0.155) |
| Eligibility first: the real engine with the model shows no product the rules exclude (2,000 test contexts) | pass (0) |
| Reasons unchanged: every shown product has exactly rules-v1's reasons (same contexts) | pass (0 differ) |
| Added inference p95 <= 2 ms (5% of the 40 ms endpoint budget, #343) | pass (0.035-0.038 ms) |
| The candidate has a served form (added after the first run; only the LR has one) | pass |

**Promoted.** The run is deterministic: the second run (`make recommendation-model`) reproduced every
metric of the first.

### Sensitivity (the same trained models, a held-out perturbed population)

Seed 1042, 1,000 customers, every behavioural and life-stage effect x0.7, every base logit +0.3; test months.

| Ranker | NDCG@3 | hit-rate@3 |
|---|---|---|
| `rules-v1` | 0.512 | 0.734 |
| `sim-propensity-v2` | 0.665 | 0.840 |
| `sim-lgbm-ranker-v2` | 0.656 | 0.821 |
| popularity | 0.605 | 0.821 |

The order holds when the assumed effects are weaker, with smaller margins.

## Intended use and limits

- Intended: re-order the eligible products for one customer, labelled as trained on simulated uptake in the
  API response (`score_version`, `score_from_simulated_data`), the customer's card list (a fixed note) and the
  assistant's reply.
- Not for: credit decisions, pricing, comparing customers (the RM list stays on rules-v1, ADR-0041), or any
  claim about real take-up.
- **The labels are our own formula.** The win over rules-v1 means the model recovers this simulation's
  assumptions better than rules-v1's hand weights do. Had the assumptions agreed with rules-v1, rules-v1
  would win (ADR-0049's `RULES_FOLLOW` check showed exactly that). The population, effect sizes and base
  rates are invented; base rates are inflated.
- The context derivation in `panel.py` approximates the API's (three-month averages as in the analytics
  module; a simplified four-part health score; a behavioural stand-in for the k-means segment).
- Clicks and the age band drive the labels but are not model inputs, so part of the signal is invisible to
  every ranker by design.
- No exposure log: real data would also carry selection bias from what was shown. The decision record lists
  what real data would need.

## Reproduce

`make recommendation-model`: exports the panel and the perturbed variant with
`python -m pbip_datagen uptake` into `data/recommendations/` (git-ignored), then
`python -m pbip_ml.recommendations.train`, which writes `models/recommendations/report.json` always and
`propensity_model.json` plus `manifest.json` only if the gate passes; it logs one MLflow run per model to
`pbip-recommendations` (local store when the server is down). About 2.5 minutes on the laptop above.

## Fairness (ADR-0015)

`age_band` is a driver of the simulated labels (life stage), never a model input and never used for ranking.
Income enters through log income, as in rules-v1.

## History

`sim-propensity-v1` (ADR-0049, `uptake-sim-1`) was not promoted: its macro PR-AUC gain over rules-v1 was
+0.014 against a +0.02 gate. Its simulation applied the eligibility rules inside the label generator; the
owner asked for a rebuild in data-gen with labels independent of the rules and a ranking gate (NDCG@3,
hit-rate@3), which is `v2` above. A `v1` artefact is refused by the `v2` loader (the version must match).
