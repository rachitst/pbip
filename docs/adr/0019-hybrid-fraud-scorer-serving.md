# ADR 0019: Serving the hybrid fraud scorer on the transfer and bill-payment paths
- Status: Accepted
- Date: 2026-11-01
- Deciders: Architecture Team

## Context
TASK-029..032 produced a LightGBM classifier, an Isolation Forest and a hybrid combiner
(`pbip_ml.fraud.hybrid.HybridScorer`: calibrated probability + anomaly score + rule points, stacked weights,
hard-rule floors, SHAP reasons). Offline it reaches recall 0.957 at REVIEW on the synthetic test period
(`docs/ml/fraud-models.md`). Production traffic is still scored by `BaselineRulesScorer`
(`baseline-rules-0.2`), whose decisions the transfer, bill-payment and fraud tests pin down. TASK-033 puts the
hybrid on the request path. Five questions had to be settled first (handoff on #56).

## Decision
1. **Feature flag.** `FRAUD_SCORER=rules|hybrid`, default `rules`. `hybrid` also needs `FRAUD_MODEL_DIR`.
   Nothing changes for a deployment that sets neither.
2. **One rule source, baseline as a floor.** The baseline rules stay the bank's policy and the fail-safe.
   With the hybrid on, a transfer gets **both** scores and the higher one decides:
   `risk = max(baseline_risk, hybrid_risk)`. The hybrid's own rule term uses the `pbip_ml` rule registry
   because its stacked weights were fitted on those points; reusing the baseline points would make the
   fitted weights meaningless. The model can raise a decision, never lower one the rules reached. So every
   decision the current tests expect still holds, and the reasons are the union (deduplicated by code).
3. **Fail-safe to rules only, never ALLOW-all.** The adapter returns the baseline result, adds a
   `MODEL_UNAVAILABLE` reason and sets `model_version` to `<baseline>+rules-only` when any of these happen:
   - the models are not loaded (no directory, missing file, load error);
   - the model raises;
   - scoring takes longer than `FRAUD_MODEL_BUDGET_MS` (default 50 ms). Model scoring runs in a small
     thread pool with `future.result(timeout=...)`, so the request really stops waiting for it.
4. **Load once at startup.** The app's lifespan loads the artifacts into `app.state` once. A request never
   reads a model file. Loading:
   - `lightgbm.txt` uses LightGBM's text format;
   - `hybrid_params.json` is JSON;
   - `isolation_forest.joblib` is a **pickle**. It is loaded only from the operator-configured
     `FRAUD_MODEL_DIR`, never from a request, a user upload or a model-chosen path. The training CLI writes
     `manifest.json` with the SHA-256 of each file, and the loader refuses files that don't match it. This
     guards against corruption and partial copies. It is **not** protection against an attacker who can
     write to the directory (they could rewrite the manifest too). That residual risk is recorded in the
     threat model (T29).
5. **Online features from the platform's own records**, queried in `modules/fraud/` (API side). This
   respects the module-boundary rule: other modules are used only through their models, for typed queries.
   The `pbip_ml` online feature function is the one proven identical to training (TASK-024):
   - prior debits for 30 days: ledger `transactions` from the customer's accounts, any status, excluding the
     transfer being scored;
   - payee added-at: `payees.created_at`;
   - failed sign-ins in 24 hours: `security_events` (`LOGIN_FAILURE` for the signed-in user);
   - device: `TransactionContext.device_id` when the platform knows it. Until TASK-095 wires the device
     registry into payments, there is no device, so `new_device = 0` and `distinct_devices_7d` comes from
     earlier fraud-event snapshots (0 today). This is stated, not hidden: F3 (account takeover) relies on
     these signals and gets them in TASK-095.
6. **Same outcomes for bill payments.** Bills already call the same `get_fraud_scorer` seam, so they get the
   same score. The decision mapping (ALLOW / STEP_UP / REVIEW / BLOCK onto `POSTED`, `PENDING_STEP_UP`,
   `PENDING_REVIEW`, `DECLINED`) is unchanged by this ADR. A STEP_UP for a customer without MFA still
   becomes REVIEW.
7. **No contract change.** `pbip_ml.fraud.contract` is untouched. The API imports a new
   `pbip_ml.fraud.inference` module (load + score one transfer). It does not import the training CLI.

## Alternatives considered
- **Replace the baseline with the hybrid.** Rejected. Decisions on real-looking cases (a 1 lakh transfer, a new
  country) would depend on a model trained only on synthetic data, and existing tested behaviour would change
  without evidence that it should.
- **Retrain the hybrid on baseline rule points.** Rejected. Some baseline rules (`NEW_COUNTRY`,
  `LARGE_AMOUNT`) have no counterpart in the labelled data, so their weights could not be learnt honestly.
- **Load the model per request (or lazily on first use).** Rejected: file I/O and unpickling on the money
  path, and the first customer of the day pays the load time.
- **Serialise the Isolation Forest without pickle (e.g. skops).** Deferred. It adds a dependency, and the
  pickle is never reachable from user input.

## Consequences
- The p95 latency target (< 100 ms) now covers baseline queries + feature queries + model scoring. It is
  measured in the TASK-033 PR, and the budget caps the model part.
- `fraud_events.model_version` shows which path decided each transfer (`fraud-hybrid-…`, or
  `baseline-rules-0.2+rules-only` on fallback), and `features` stores the full snapshot, so every decision can
  be reconstructed.
- The evaluation report (TASK-039) must describe the served combination (hybrid with the baseline as floor),
  not the hybrid alone.
- **Measured on the laptop** (Celeron N4500), served end to end (rules + DB look-ups + models, 25 transfers):
  p50 20.6 ms, p95 24.7 ms, max 29.2 ms. Model scoring alone: p50 15 ms, p95 31 ms; a warm-up score at
  load avoids the 34 ms cold first call.
- **Default stays `rules`.** The synthetic training data has no legitimate transfers to new payees: normal
  debits go to payees at least 32 days old, while 83% of fraud debits use a payee under 72 h old. So the
  served model flags almost any transfer to a payee added this month: a ₹10 transfer to a 48 h-old payee
  scored risk 75 (REVIEW). The hybrid path is implemented and tested, but switching it on needs the data
  fix in #251 and a retrain first.
  **Update (1 Nov 2026, #251 fixed):** with generator v2.1 and retrained models, legitimate new-payee
  transfers are held at REVIEW 0.09% (test) / 0.24% (held-out) of the time. Local and demo now run
  `hybrid` (`.env.example`, docker-compose); the code default stays `rules`.
- Velocity counters move to Redis in TASK-034, and login-failure/device rules join the registry in TASK-095.
  Both feed this same adapter.
