# Alignment with AI governance guidelines for financial institutions (TASK-088)

This page shows how the models in PBIP line up with widely used governance principles for AI in financial
services. Each claim points to a file, test, ADR or report on `main`. Gaps are listed at the end.

PBIP is a simulated bank built for a course. Nothing here claims legal compliance. The frameworks are used
as a checklist of what a regulated bank would be asked for.

## Frameworks used

| Framework | What it is | Principles used here |
|---|---|---|
| **RBI FREE-AI** (Framework for Responsible and Ethical Enablement of AI, report of the RBI committee, Aug 2025) | Recommendations for AI in the Indian financial sector. A committee report, not a binding direction | Its seven "Sutras": trust is the foundation; people first; innovation over restraint; fairness and equity; accountability; understandable by design; safety, resilience and sustainability |
| **EU AI Act** (Regulation (EU) 2024/1689) | Obligations for high-risk AI systems | Risk management (Art. 9), data and data governance (Art. 10), technical documentation (Art. 11), record-keeping (Art. 12), transparency to deployers (Art. 13), human oversight (Art. 14), accuracy, robustness and cybersecurity (Art. 15) |
| **SR 11-7** (US Federal Reserve / OCC, 2011) and **PRA SS1/23** (UK, 2023) | Model risk management for banks | SS1/23's five principles: model identification and risk classification; governance; development, implementation and use; independent validation; risk mitigants. SR 11-7: conceptual soundness, ongoing monitoring, outcomes analysis, effective challenge |

Scope under the EU AI Act: Annex III lists credit scoring of natural persons as high-risk but excludes AI
used to detect financial fraud. No PBIP model scores creditworthiness. So no PBIP model would be high-risk
under the Act. We still apply the high-risk obligations to the fraud scorer, because it can hold or decline
a customer's payment.

## Model inventory and materiality (SS1/23 principle 1)

| Model | Card | Effect on a customer | Materiality (our assessment) |
|---|---|---|---|
| LightGBM fraud classifier | [lightgbm-fraud.md](lightgbm-fraud.md) | Through the hybrid, can step up, hold or decline an outgoing payment | High |
| Isolation Forest anomaly model | [isolation-forest.md](isolation-forest.md) | Through the hybrid, as above (small weight) | High (as a component) |
| Hybrid fraud combiner | [hybrid-fraud-combiner.md](hybrid-fraud-combiner.md) | Makes the decision: ALLOW / STEP_UP / REVIEW / BLOCK | High |
| Balance forecast `forecast-arima-2` | [balance-forecast.md](balance-forecast.md) | Shows a prediction and may send a low-balance alert. Decides nothing | Low |
| Recommendation score `rules-v1` | [recommendation-score.md](recommendation-score.md) | Orders eligible products. Approves or prices nothing | Low to medium (marketing suitability) |
| K-Means segmentation | [kmeans-segmentation.md](kmeans-segmentation.md) | Analytics persona and one input to recommendations | Low |

The cards are the inventory. Every served model returns its `model_version`, except K-Means (see gaps).

## 1. Fairness and bias

FREE-AI "fairness and equity"; EU AI Act Art. 10 (bias examination of data).

| Model | Evidence |
|---|---|
| Fraud (LightGBM, Isolation Forest, hybrid) | Behavioural transaction features only: 9 LightGBM features (`MODEL_FEATURES`), 4 anomaly features (`ANOMALY_FEATURES`) in `ml/src/pbip_ml/fraud/training.py`. No demographic field. `NEW_COUNTRY` is a coarse GeoIP security signal (ADR-0014) |
| Forecast | The account's own balance history only (`ml/src/pbip_ml/forecasting/model.py`) |
| Recommendations `rules-v1` | The only demographic input is the consented `age_band`. It adds an eligibility reason and never changes ranking (ADR-0015 decision 2). Tested by `test_adr_0015_age_band_never_changes_ranking_only_adds_an_eligibility_reason` and `test_unknown_age_band_still_receives_standard_recommendations` (`ml/tests/test_recommendations.py`) |
| K-Means | Five ledger features (`FEATURE_NAMES` in `ml/src/pbip_ml/segmentation/pipeline.py`). The card warns that HIGH_NET_WORTH reflects wealth and must not restrict product access |
| All | ADR-0015 decision 3: gender, marital status, religion, caste, ethnicity, health, precise location and declared income are **not collected** |

## 2. Explainability and transparency

FREE-AI "understandable by design"; EU AI Act Art. 13; AGENTS.md section 8 "no black boxes".

| Model | Evidence |
|---|---|
| Fraud | Every decision returns `risk_score`, `decision`, `reasons[]` (source RULE / MODEL / ANOMALY) and `model_version`. MODEL reasons are the top-3 positive TreeSHAP contributions: `test_shap_reasons_are_top3_positive_exact_and_fast`, `test_every_scenario_gets_unified_readable_reasons` (`ml/tests/test_fraud_hybrid.py`); `test_new_payee_plus_large_amount_is_held_with_human_readable_reasons` (`apps/api/tests/integration/test_transfers.py`). Staff see "Why was this flagged?" (`GET /api/v1/fraud/alerts/{alert_id}`, `apps/web/src/pages/staff/FraudAlert.tsx`). Customers never see reasons or scores, so fraudsters cannot probe the model: `test_block_declines_without_moving_money_and_hides_the_reasons` |
| Forecast | Every response is labelled "Predicted, not exact", carries 80% and 95% intervals and `model_version`: `test_forecast_is_labelled_with_intervals_from_the_account_history` (`apps/api/tests/integration/test_forecast.py`) |
| Recommendations | Each product carries reasons built from the customer's own INR figures and `model_version` `rules-v1`. The formula is readable code (`relevance_score` in `ml/src/pbip_ml/recommendations/engine.py`). `test_catalogue_makes_no_unpublished_rate_or_approval_claims` |
| K-Means | Each segment has a confidence and an explanation relative to the population median (card "Intended use") |
| Assistant (not a model card, uses an LLM) | The LLM only picks an approved tool. The server writes the answer from service results, so figures are exact (ADR-0018). The response says which engine answered (`engine: llm \| rules`) |

## 3. Accountability and human oversight

FREE-AI "accountability" and "people first"; EU AI Act Art. 14; SS1/23 principle 2.

- **Analyst review of fraud holds (TASK-036).** REVIEW holds the funds. A fraud analyst with MFA
  (`require_roles(FRAUD_ANALYST, ADMIN, mfa=True)`, `apps/api/src/pbip_api/modules/fraud/router.py`)
  releases or rejects the transfer. Tests: `test_review_holds_funds_and_analyst_release_posts_the_transfer`,
  `test_analyst_reject_releases_the_hold_and_declines`,
  `test_analyst_decision_records_who_why_and_tells_the_customer` (`apps/api/tests/integration/test_transfers.py`);
  `test_the_analysts_decision_settles_the_held_bill_payment` (`test_bills.py`).
- **Escalation to a human.** STEP_UP without MFA, or after three wrong codes, goes to an analyst:
  `test_step_up_without_mfa_escalates_to_analyst_review`,
  `test_three_wrong_step_up_codes_hand_the_transfer_to_an_analyst`.
- **Rules stay policy.** `risk = max(baseline rules, hybrid)`. The model can raise a decision, never lower one
  (ADR-0019 decision 2): `test_the_model_can_raise_but_never_lower_the_rules_decision`
  (`apps/api/tests/integration/test_fraud_hybrid.py`).
- **No automated decision on credit or eligibility.** Forecast, recommendations and segments decide nothing
  about the customer (each card's "Out of scope").
- **Named owner and review.** `.github/CODEOWNERS` makes `rachitst` the owner of every ML file. Every PR needs
  review and the CI Gate (AGENTS.md section 11).

## 4. Robustness, safety and fail-safe

FREE-AI "safety, resilience and sustainability"; EU AI Act Art. 15; SS1/23 principle 5 (risk mitigants).

| Control | Evidence |
|---|---|
| Rules-only fail-safe, never ALLOW-all (ADR-0019 decision 3) | `test_fails_safe_to_rules_only_never_allow_all`, `test_a_model_that_raises_also_falls_back` (`ml/tests/test_fraud_hybrid.py`); `test_any_model_problem_falls_back_to_the_rules_never_allow_all`, `test_load_engine_serves_rules_only_without_valid_artifacts`, `test_a_broken_model_stack_never_stops_the_api_from_starting` (`apps/api/tests/integration/test_fraud_hybrid.py`) |
| Latency budget (50 ms, contract 100 ms) | `test_real_model_slower_than_the_budget_falls_back_to_the_rules` and `test_fraud_model_budget_is_bounded_by_the_scoring_contract` (the timeout path and the 5-100 ms setting, deterministic); real latency and fallback rate measured by `scripts/benchmark_fraud_latency.py` (#362), not by a test; latency p95 24.7 ms served (`docs/ml/fraud-models.md` "Serving") |
| Behaviour-regression suite | `ml/tests/test_fraud_behaviour_regression.py`: `test_known_app_scam_scores_at_least_80`, `test_routine_grocery_payment_scores_at_most_25`, `test_no_change_lowers_a_known_fraud_score` |
| Train/serve parity | `test_served_row_equals_the_training_row`, `test_loaded_scorer_scores_exactly_like_the_in_memory_one` (`ml/tests/test_fraud_inference.py`) |
| Tampered artifacts | SHA-256 manifest check: `test_damaged_artifacts_are_refused`; threat model T34 (`docs/security/threat-model.md`) |
| Training off the request path | `apps/api/tests/unit/test_training_code_stays_off_the_request_path.py` (ADR-0007, ADR-0012) |
| Assistant LLM | Guards before any model call, read-only tools, deterministic fallback (ADR-0018). Adversarial suite `apps/api/tests/integration/test_assistant_adversarial.py` (injection, cross-customer, SQL) |
| Forecast | Short histories use naive methods (`test_method_depends_on_history_length`); intervals nested and ordered (`test_forecast_has_ordered_nested_intervals`, `ml/tests/test_forecasting.py`) |

## 5. Data governance and privacy

FREE-AI "trust is the foundation"; EU AI Act Art. 10.

- **Synthetic, seeded, documented data.** `pbip_datagen` (ADR-0009). Fraud: seed 42 for training, seed 4242 as
  held-out variant (`docs/ml/model-evaluation-report-v1.md` §1-§2).
- **Time-based split, no leakage.** `test_split_is_chronological_with_no_overlap` (`ml/tests/test_fraud_training.py`);
  forecast backtest uses only past data per origin: `test_rolling_origin_uses_only_past_data_for_each_origin`.
- **Consent-gated demographics (ADR-0015).** Date of birth is optional and needs explicit, withdrawable consent.
  The engine sees only the band. Tests in `apps/api/tests/integration/test_demographics.py`:
  `test_new_customers_have_nothing_inferred`,
  `test_consent_grant_and_withdrawal_are_recorded_and_audited_with_the_policy_version`,
  `test_date_of_birth_never_reaches_the_logs`, `test_staff_responses_never_contain_the_date_of_birth`;
  `test_recommendations_use_the_customers_real_figures_and_ignore_date_of_birth_without_consent`
  (`test_recommendations.py`).
- **Own data only (object authorization).** `test_bola_forecast_neither_reads_nor_alerts_for_another_customers_account`
  (`apps/api/tests/integration/test_rbac_bola.py`), `test_recommendations_are_scoped_to_the_authenticated_customer`.
- **Assistant has no data access of its own.** No SQL, no session; `customer_id` comes from the server, never
  from the request (ADR-0018; `test_customer_ids_in_the_request_body_are_rejected`). Every tool call is
  audited without payloads: `test_every_tool_call_and_refusal_is_audited_without_payloads`.

## 6. Monitoring and drift

SR 11-7 "ongoing monitoring" and "outcomes analysis"; EU AI Act Art. 12 (record-keeping).

- **Every fraud decision is recorded** with its feature snapshot and `model_version` in `fraud_events`:
  `test_feature_snapshot_is_recorded_for_audit` (`ml/tests/test_fraud_hybrid.py`).
- **Operational telemetry for staff.** `GET /api/v1/fraud/stats` (decisions over time, hold rate, top
  reasons; `apps/api/tests/integration/test_fraud_stats.py`) and `GET /api/v1/fraud/monitoring` (TASK-099;
  `test_security_monitoring.py`).
- **Outcome labels.** An analyst's RELEASE or REJECT sets the alert status to `FALSE_POSITIVE` or
  `CONFIRMED_FRAUD` (`apps/api/src/pbip_api/modules/fraud/models.py`), so labelled outcomes build up. No
  pipeline uses them for retraining yet.
- **Feature drift (PSI, TASK-068): computed but not scheduled.** PR #336 added PSI for amount and velocity
  (`pbip_ml/monitoring/psi.py`, warning 0.1, critical 0.25), but nothing runs it or sets the
  `pbip_feature_psi` gauge yet, and there is no Grafana panel or alert (GitHub issue #324). See gaps.

## 7. Documentation and model-risk management

EU AI Act Art. 11; SS1/23 principles 3 and 4; SR 11-7 "conceptual soundness".

- **Model cards** for every model, with metrics copied from cited reports ([README.md](README.md)).
- **Evaluation report** `docs/ml/model-evaluation-report-v1.md`: precision, recall, F1, PR-AUC, FPR and confusion
  matrices (§3), never accuracy alone; cost analysis (§4); latency (§5); limitations of synthetic data (§7);
  generator v2.2 results (§8). Forecast backtest: `docs/ml/balance-forecast.md` (sMAPE, interval coverage).
- **Reproducible runs.** Deterministic training (`test_training_is_deterministic`); fraud runs are logged to
  MLflow (`test_cli_run_writes_artifacts_and_logs_to_mlflow`).
- **Measured promotion gate.** `make promote-model` re-measures the run itself before moving the `champion`
  alias (`ml/src/pbip_ml/fraud/promotion.py`, TASK-038):
  `test_bad_run_fails_the_real_evaluation_although_its_report_claims_099`,
  `test_challenger_worse_than_the_champion_is_rejected_and_champion_stays`,
  `test_export_refuses_a_champion_that_did_not_pass_the_gate` (`ml/tests/test_fraud_promotion.py`).
- **Decisions recorded as ADRs:** ADR-0007 (contracts in process), ADR-0009 (dataset), ADR-0012
  (segmentation offline), ADR-0014 (GeoIP), ADR-0015 (demographics), ADR-0018 (assistant), ADR-0019 (hybrid
  serving).
- **Versioning.** A change to forecast or recommendation logic must bump its `MODEL_VERSION` (cards,
  "Retraining and promotion").

## Gaps (stated, not hidden)

1. **Synthetic data only.** All metrics measure how well the models learn planted patterns, not real-world
   performance (report §7). The hybrid's stacking weights and bands must be re-fitted on real labelled
   outcomes before anyone relies on them.
2. **No fairness audit across protected attributes.** PBIP collects none beyond the consented age band
   (ADR-0015), so there is nothing to audit against. No proxy analysis has been done either. Monetary
   features correlate with income, and `NEW_COUNTRY` uses location.
3. **No independent validation** (SS1/23 principle 4, SR 11-7 "effective challenge"). One CODEOWNER owns all
   ML code. PR review is peer review, not a separate validation function.
4. **No scheduled input-drift monitoring.** PSI exists (PR #336), but no job runs it or exports it until
   GitHub issue #324. There is no scheduled retraining and no automated outcome-based performance tracking.
5. **No affected-person explanation or contest route for fraud holds.** Customers are told the outcome but not
   the reasons (deliberate, for security). There is no in-app dispute flow for a declined payment.
6. **Weaker model-risk controls for the non-fraud models.** No MLflow tracking or registry for forecast,
   recommendations or K-Means. K-Means has no `model_version` and no quality gate (ADR-0012). The
   `rules-v1` weights are expert judgement, with no outcome evaluation.
7. **Known fraud-model blind spots.** F4 (geo jump) is partly missed, F7 (structuring) is not detected, and
   device signals are not yet on the payment path (TASK-095) (hybrid card "Limitations").
8. **No institution-level governance.** There is no AI policy, model-risk policy, risk appetite or AI incident
   process, because PBIP has no board or second line. The model inventory and materiality ratings above are
   the project team's own view.
9. **Residual artifact risk.** Anyone who can write `FRAUD_MODEL_DIR` can also rewrite the manifest
   (threat model T34).
