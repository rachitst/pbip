# ADR 0051: Fraud feature contract 2: a structuring window and the time since the last activity
- Status: Accepted (under owner's standing approval, 2026-11-04)
- Date: 2026-11-04
- Deciders: Architecture Team (issues #274 F7 and #275 F8)

## Context
ADR-0046 (PR #424) added the STRUCTURING and DORMANT_REACTIVATION rules to the API's baseline. The model behind
the hybrid score (`pbip_ml.fraud`, ADR-0019) still had no input for either pattern:

- **F7 structuring.** Nothing counted movements just under the INR 1,00,000 reporting threshold. The generator's
  F7 deposits were labelled but not detected (evaluation report §8).
- **F8 dormant-account reactivation.** Nothing measured the time since the account was last used. F8 was caught
  only through takeover signals (new device, new payee, maximum amount), so a dormant-account fraud without them
  would be missed.
- **No legitimate credits.** Until generator v2.4 every credit in the training data was fraud (F6, F7), so no
  false-positive rate on legitimate credits could be measured, and a credit-side feature would have learnt
  "any credit is fraud".

The offline (training) and online (serving) feature paths in `pbip_ml.fraud.features` must agree exactly
(TASK-024). Changing `FEATURE_NAMES` is a change of the contract between the platform and the ML package.

## Decision
1. **Two new features**, in both paths, with the point-in-time rule (strictly earlier events only):
   - `near_threshold_72h`: prior customer activity in the last 72 h, received or sent, each at least 90% of the
     INR 1,00,000 threshold and below it (integer arithmetic, `features.near_threshold`). The scored transfer is
     not counted: its amount is already the `log_amount` input.
   - `days_since_last_activity`: whole days since the customer's last prior activity, `-1` if there is none.
2. **Activity** is what the customer did: TRANSFER, DEPOSIT, BILL_PAYMENT and FD_BOOKING postings that are POSTED,
   the same set as ADR-0046's DORMANT_REACTIVATION. Fees, deposit interest, maturity payouts, reversals and loan
   postings are the bank's doing; they do not wake a dormant account. `PastTransfer.activity` carries this flag
   (default `True`: every generated row is customer activity). The two new features read only activity rows;
   the existing features are unchanged in the API.
3. **`distinct_devices_7d` counts debits only.** A credit is made by the sender, so the API never had a customer
   device for it, while the generator's credits carry the customer's usual device. With legitimate credits in
   the data (v2.4), counting them offline would be train/serve skew for customers who changed phones.
4. **What the API reads** (`modules/fraud/hybrid.py`): as before, the customer's debits in 30 days with their
   devices, now with the activity flag; plus money received in 30 days from anyone but the customer's own
   accounts (a move between them is already a debit), without a device; and, only when those 30 days hold no
   activity, the latest activity before them on each side (two index range scans that each stop at the first
   row). Read-only, no lock, existing `(debit|credit)_account_id, created_at` indexes, no migration.
5. **A model is served with its own columns.** LightGBM matches columns by position, so the scorer cuts the row
   to `booster.feature_name()` (`hybrid.model_columns`). The `-3` artifacts (9 features) therefore keep scoring
   correctly with this code, and the export of a new model does not have to happen at the same moment as the
   deploy. `test_a_model_trained_before_the_new_features_still_scores` pins this. (Merged with #379's single-matrix scoring: the matrix is built from the booster's columns.)
6. **Version**: `HYBRID_VERSION = hybrid-lgbm-iforest-4` for every model trained with this contract. Whether
   `-4` replaces `-3` in service is decided by the measurements below and the TASK-038 gate.
7. **Generator v2.4**: legitimate inbound transfers (L4): about one a month from regular senders and about one
   large one-off a year (0.5-3x monthly income, capped at INR 2,00,000). They are drawn last, on their own random
   stream and id namespace, so every v2.3 row and label is unchanged (`test_v2_3_rows_are_unchanged_by_money_received`).

## Evidence
Retrained with the production recipe on generator v2.4 (`model-evaluation-report-v1.md` §11, MLflow run
`0930d6415eec4b7eaa6bf59c5689bd91`). `-3` was scored on the same rows. Results for test / held-out 6 months /
held-out 12 months: hybrid PR-AUC 0.945 / 0.887 / 0.737 -> 0.943 / 0.931 / 0.839; recall at REVIEW 0.923 /
0.894 / 0.848 -> 0.942 / 0.934 / 0.893. Legitimate debits held at REVIEW: 4 / 15 / 35 -> 5 / 18 / 27, all under the
0.1% bar. New-payee bars: unchanged or lower. F8 recall at REVIEW on the 12-month held-out set: 0.824 -> 0.882.

Limits, stated plainly:
- `near_threshold_72h` has zero SHAP weight, because F7's labelled rows are credits and the outgoing scorer has no
  positive outgoing F7 row to learn from. F7 detection stays with the rule (ADR-0046) and the deposit-side monitor.
- Most of `days_since_last_activity`'s weight is the 0-day gap of multi-row synthetic scenarios. That gap may not
  carry over to real traffic.

`-4` is better on the held-out sets, but it **failed the TASK-038 gate**: on the same test rows its PR-AUC was 0.9433
against the champion's 0.9446. So `-3` stays served, and this code serves it unchanged. The contract stays in place,
so a future retrain with real or richer labelled data can use the new features.

## Consequences
- The API runs one more short query per scored transfer (credits in 30 days) and, for an account with no
  activity in 30 days, two more single-row index scans.
- Training and evaluation data now contain legitimate credits; the scorer still trains and evaluates on
  outgoing transfers only (#261), but their history windows include the credits, as online.
- `FraudFeatures` has two more required fields. Code that builds it by hand (tests, the benchmark) must set them.
- The threshold and band are constants of the feature contract, not settings: a model is trained for one
  definition. They match ADR-0046's defaults (`FRAUD_STRUCTURING_*`); changing the rule's settings does not
  change the feature, and changing the feature needs a retrain and a new version.

## Alternatives considered
- **A credit-sum feature (all money received in a window).** It also moves with salary and F6 mule inflows; the
  near-threshold count is the F7 signature itself and is explainable in one sentence.
- **`days_since_last_activity` from every posting.** Fees and interest would keep a dormant account "active",
  so the feature would be useless exactly where F8 happens. The rule (ADR-0046) made the same choice.
- **Pass the last activity time as a separate input instead of a history row.** It would move the definition out
  of the feature module, and the parity test could no longer prove it.

## Standing-approval conditions
(1) No security, auth, RBAC, BOLA or audit change; the model can only raise a decision above the rules (ADR-0019).
(2) No money movement: the scorer's reads are read-only, no lock. (3) No schema change or migration.
(4) No new service, dependency or outbound data. (5) Every number is measured and reproducible (seeded data,
deterministic training) and labelled synthetic. (6) Within Blueprint §8 F7/F8 and issues #274/#275.
