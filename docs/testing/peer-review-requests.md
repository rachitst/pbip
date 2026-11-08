# Peer review requests

Every PR below is already merged on `main` after CI and the lead's line-by-line review. This list asks each teammate
for a second, independent review of recent merged work in their own area, recorded on GitHub, so that each area has a
reviewer who knows it besides the lead. The review process itself is described in
[CONTRIBUTING.md section 4](../../CONTRIBUTING.md#review) and the
[test strategy, section 12](test-strategy-and-report.md#12-code-review-process).

## How to review a merged PR

1. Open the PR link and read the description first: what it claims, which tests prove it, what it says is not done.
2. Read the **Files changed** tab, starting with the files named in "What to look at".
3. Leave comments on specific lines for anything wrong, unclear or untested. A question is a valid comment.
4. Finish with **Review changes**: **Approve** if the change is correct as merged, or **Comment** with your findings.
   Do not use "Request changes" on a merged PR; open an issue for every real defect instead and link it in your review.
5. Tick your row in the tracker at the end of this page in a docs PR, or tell the lead, who records it in a batch docs PR.

A merged PR can still be reviewed and approved on GitHub; the review is kept on the PR as evidence.

**Common checklist** (applies to every PR; the area lists below add to it):

- [ ] The description's claims match the diff, and every acceptance criterion has a test or artefact.
- [ ] New behaviour has tests; a bug fix has a regression test that fails without it.
- [ ] No TODO, placeholder or stub code; no `Any`, `# type: ignore`, `any` or `@ts-ignore` without a reason.
- [ ] No secrets, personal data, tokens or full account numbers in code, tests, logs or fixtures.
- [ ] The architecture holds: router, service, policy or repository, models; other modules used only through their
      service or policy functions (AGENTS.md section 2).
- [ ] Docs, ADR, OpenAPI contract and threat-model row updated where the change needs them.

## Kalpak: web UI

| PR | Title | Link | What to look at |
|---|---|---|---|
| #300 | feat(web): registration and MFA enrolment screens (#201) | https://github.com/rachitst/pbip/pull/300 | `pages/Register.tsx`, `pages/MfaEnrolment.tsx` and their tests; `lib/password.ts`; the route changes in `shell/routes.tsx` |
| #306 | feat(web): scheduled transfers screen on the TASK-015 engine (TASK-021) | https://github.com/rachitst/pbip/pull/306 | `pages/ScheduledTransfers.tsx` (527 lines) and `ScheduledTransfers.test.tsx`; pause, resume and cancel states; the API client additions |
| #328 | feat(web): product recommendations carousel on the accounts page (TASK-062) | https://github.com/rachitst/pbip/pull/328 | `pages/ProductRecommendations.tsx`, `lib/recommendations.ts`, the keyboard disclosure for reasons, `ProductRecommendations.test.tsx` |

Area checklist:

- [ ] Loading, error and empty states are all handled and tested.
- [ ] Data is fetched only through react-query and the typed client; response types come from `schema.d.ts`, never
      hand-written.
- [ ] Money is formatted with `lib/money.ts`; no floats.
- [ ] Keyboard and screen-reader use: labels, focus order, `aria` attributes on dialogs and disclosures.
- [ ] Works at phone width (the layout rules in `docs/design/ui-guidelines.md`).
- [ ] No secrets (TOTP secret, recovery codes) kept beyond the screen that shows them.

## Krishna: analytics

| PR | Title | Link | What to look at |
|---|---|---|---|
| #318 | feat(analytics): end-of-month balance projection and forecast chart (TASK-043, TASK-050) | https://github.com/rachitst/pbip/pull/318 | `analytics/projection.py`, `analytics/service.py`; `test_forecast_projection.py`, `test_balance_projection.py`; the BOLA matrix rows |
| #325 | feat(dashboards): compliance exceptions and behavioural analytics dashboards (TASK-100, TASK-101) | https://github.com/rachitst/pbip/pull/325 | `analytics/behaviour.py`, `compliance/exceptions.py`, the new routes and their role gates; `test_behaviour_analytics.py`, `test_compliance_exceptions.py`, `test_route_policy_registry.py` |
| #458 | perf(events): cap a clickstream batch at 25 events so p95 stays under 15 ms (#78, ADR-0055) | https://github.com/rachitst/pbip/pull/458 | `events/schemas.py` (the cap), `test_events.py`, ADR-0055 and `docs/testing/events-latency-2026-11-04.md` (do the numbers support the decision?) |

Area checklist:

- [ ] Aggregates are correct at the edges: empty months, reversed transactions, own-account moves, time zones (IST bank
      day against UTC).
- [ ] Integer minor units end to end; ratios and percentages computed without float money.
- [ ] Staff dashboards return aggregates and ids only, never personal data the role should not see.
- [ ] Query cost is bounded (indexes, limits) and measured where the PR claims a latency.
- [ ] Deterministic tests: injected clock, seeded data, no sleeps.

## Abbas: assistant and documentation

| PR | Title | Link | What to look at |
|---|---|---|---|
| #368 | feat(assistant): optional Groq router for unclear questions with Ollama and rules fallback (ADR-0037) | https://github.com/rachitst/pbip/pull/368 | `assistant/providers.py`, `sanitize.py` (what leaves the stack), `core/ratelimit.py`, `core/config.py` validation; `test_assistant_groq.py` (unit and integration) and the adversarial additions |
| #335 | feat(assistant): FAQ, product discovery and investment suggestion tools (TASK-105) | https://github.com/rachitst/pbip/pull/335 | `assistant/faq.py`, `discovery.py`, `tools.py`, `fallback.py`; migration `0024`; `test_assistant.py` |
| #374 | docs(user-manual): customer, staff and compliance guides with screenshots (TASK-092) | https://github.com/rachitst/pbip/pull/374 | `docs/user-manual/*.md`: does every step match the screens and the code today? |

Area checklist:

- [ ] The model never receives SQL, a session, identity, tool results or history; `customer_id` comes only from the
      access token (AGENTS.md section 9).
- [ ] Tool arguments are enums or bounded values, validated by the tool's own model after the LLM answers.
- [ ] Every failure path falls back safely (Groq, then Ollama, then rules) and is audited without payloads.
- [ ] Retrieved text is treated as untrusted: delimited, length-capped, control characters stripped.
- [ ] Documentation: every path, command and screenshot exists; no secrets or real personal data in screenshots.

## Pranay: machine learning

| PR | Title | Link | What to look at |
|---|---|---|---|
| #437 | feat(fraud): F7/F8 features, legitimate inbound transfers, -4 retrain (gate failed, -3 stays) (#274, #275, ADR-0051) | https://github.com/rachitst/pbip/pull/437 | `ml/src/pbip_ml/fraud/features.py`, `fraud/hybrid.py` (API side), `test_fraud_feature_parity.py`; ADR-0051 and the evaluation report: is the gate decision honest? |
| #376 | feat(fraud): hourly PSI drift evaluation feeding pbip_feature_psi, with Grafana panel and alerts (#324) | https://github.com/rachitst/pbip/pull/376 | `fraud/drift.py`, the beat entry in `worker/celery_app.py`, `test_feature_drift.py`, `test_feature_drift_job.py`, ADR-0038 |
| #354 | fix(ml): keep fraud training off the hybrid request path with an allow-listed artifact loader (#350) | https://github.com/rachitst/pbip/pull/354 | `ml/src/pbip_ml/fraud/artifacts.py` (the allow-list), `inference.py`, the import-linter contract in `pyproject.toml`, `test_hybrid_scorer_skips_fraud_training.py`, ADR-0033 |

Area checklist:

- [ ] Time-based splits only; no leakage of future data into features or labels.
- [ ] Offline and online features compute the same numbers (parity test).
- [ ] Metrics reported are precision, recall, F1, PR-AUC, FPR, FNR, latency and the confusion matrix, never accuracy
      alone; the limits of synthetic evaluation are stated.
- [ ] Serving fails safe to rules-only, never to allow-all; training code stays off the request path.
- [ ] Artefacts are loaded through the allow-listed loader, with checksums, never with plain `pickle`.

## Sujal: payments

| PR | Title | Link | What to look at |
|---|---|---|---|
| #296 | feat(transfers): implement scheduled and recurring transfers (TASK-015) | https://github.com/rachitst/pbip/pull/296 | `payments/scheduled_service.py`, `recurrence.py`, `models.py`, migration `0014`; the per-instruction transaction and lock order; ADR-0023 |
| #369 | fix(payments): expire unanswered step-ups and enforce the idempotency key TTL | https://github.com/rachitst/pbip/pull/369 | `payments/step_up_expiry.py`, `ledger/service.py` and `ledger/key_purge.py`, migration `0026`, `test_step_up_expiry_and_key_ttl.py` (the race with a late code), ADR-0036 |
| #364 | fix(payments): require Idempotency-Key on step-up confirmation and analyst decision | https://github.com/rachitst/pbip/pull/364 | `PaymentsService.confirm_step_up` and `review_alert`, the router headers, the new tests in `test_transfers.py` (concurrent step-ups and decisions settle exactly once) |

Area checklist:

- [ ] Lock order: user advisory lock, idempotency key, accounts `FOR UPDATE` in sorted id order, then the transaction
      header (AGENTS.md section 4).
- [ ] Balances and entries change only in `ledger/service.py`; every posting balances.
- [ ] Business failures leave no partial state; evidence that must persist is committed before the error.
- [ ] Idempotent retry, concurrent attempt, other-user (404) and validation failure are each tested.
- [ ] Migrations have a working downgrade and no data loss.

## Tracker

| Teammate | Area | #1 | #2 | #3 |
|---|---|---|---|---|
| Kalpak | Web UI | #300 [ ] | #306 [ ] | #328 [ ] |
| Krishna | Analytics | #318 [ ] | #325 [ ] | #458 [ ] |
| Abbas | Assistant and docs | #368 [ ] | #335 [ ] | #374 [ ] |
| Pranay | Machine learning | #437 [ ] | #376 [ ] | #354 [ ] |
| Sujal | Payments | #296 [ ] | #369 [ ] | #364 [ ] |

Checked on GitHub on 2026-11-08: none of the 15 PRs above has a teammate review or comment.

## v1.1.0 requests (posted on #317, 2026-11-07) and what was recorded

Each teammate was asked for three merged PRs in their area. Checked on GitHub on 2026-11-08 at 07:30 UTC; the
reviews were recorded after the PRs merged.

- **A GitHub review** means a review submitted with **Approve** on the PR.
- **A PR comment** means a conversation comment saying "Approved", not a submitted review. It still counts as a
  recorded approval.
- No review has line comments, and none links an issue.

| Teammate (login) | Area | PR | Recorded | Date (UTC) |
|---|---|---|---|---|
| Kalpak (`Patil-Kalpak`) | Web UI | #477 usage analytics | PR comment "Approved" | 2026-11-08 06:33 |
| Kalpak (`Patil-Kalpak`) | Web UI | #487 monthly SIP | PR comment "Approved" | 2026-11-08 06:33 |
| Kalpak (`Patil-Kalpak`) | Web UI | #464 fraud monitoring dashboard | PR comment "Approved" | 2026-11-08 06:33 |
| Krishna (`LetRock1`) | Analytics and recommendations | #463 behavioural profile | GitHub review, Approve | 2026-11-08 07:23 |
| Krishna (`LetRock1`) | Analytics and recommendations | #461 RTQ-1 suitability rule | GitHub review, Approve | 2026-11-08 07:23 |
| Krishna (`LetRock1`) | Analytics and recommendations | #470 loan recommendations | GitHub review, Approve | 2026-11-08 07:24 |
| Abbas (`Abbaskazi`) | Assistant and docs | #474 FAQ coverage | PR comment "Approved" | 2026-11-07 19:22 |
| Abbas (`Abbaskazi`) | Assistant and docs | #482 consent FAQ | PR comment "Approved" | 2026-11-07 19:23 |
| Abbas (`Abbaskazi`) | Assistant and docs | #476 use cases and traceability | PR comment "Approved" | 2026-11-07 19:23 |
| Pranay | Machine learning | #437 F7/F8 features and retrain | none | |
| Pranay | Machine learning | #485 fakeredis keyspace race | none | |
| Pranay | Machine learning | #479 shared trained model in tests | none | |
| Sujal (`SUJALGPM`) | Payments and loans | #465 simulated investments | PR comment "Approved" | 2026-11-08 06:06 |
| Sujal (`SUJALGPM`) | Payments and loans | #484 monthly fund SIPs | PR comment "Approved" | 2026-11-08 06:11 |
| Sujal (`SUJALGPM`) | Payments and loans | #472 E2E journeys | PR comment "Approved" | 2026-11-08 06:16 |

**Total:** 12 of 15 PRs have a teammate approval:
- 3 are GitHub reviews (all of Krishna's);
- 9 are approval comments (Kalpak, Abbas and Sujal);
- Pranay's 3 machine-learning PRs have none.

One limit applies to all 12: none has line comments. Each records that a teammate approved the change, but it doesn't
show what they checked. Sujal's #465 touches `ledger/`, so it is the CODEOWNERS case of AGENTS.md section 4.
