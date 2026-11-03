# Test strategy and verification report (TASK-087, #110)

**Date:** 3 Nov 2026. **Code under report:** `main` at `195fe1a`.

This report collects the test evidence that already exists. It runs nothing new. Every number has a label for where
it was measured:

| Label | Meaning |
|---|---|
| **[CI]** | GitHub Actions on `rachitst/pbip`. The run ID is given, and the number is copied from that run's log or artifact. |
| **[Laptop-U]** | The reference Ubuntu laptop: Intel Celeron N4500 (2 cores, 1.1 GHz), 7 GB RAM, Docker. |
| **[Laptop-W]** | The Windows 10 Pro 22H2 PC (build 19045, PowerShell 5.1) used for #329 and #371: Intel Core i5-6300U, 2 cores / 4 threads, 7.9 GB RAM. The hardware was read with `Get-CimInstance` on 3 Nov 2026 on the PC this report was written on. |
| **[Not measured]** | No measurement exists. Staging numbers wait for a staging host (#198, #101). |

The strategy (which layer proves what) is in [`PROJECT_BLUEPRINT.md` §12](../PROJECT_BLUEPRINT.md) and the rules are in
[`AGENTS.md` §10](../../AGENTS.md). This report gives the results against them.

## 1. CI runs used

| Run | Workflow | Commit | Result |
|---|---|---|---|
| [41600683787](https://github.com/rachitst/pbip/actions/runs/41600683787) | CI (all 9 jobs including `CI Gate`) | `195fe1a` | success |
| [41600683862](https://github.com/rachitst/pbip/actions/runs/41600683862) | CodeQL (`python`, `javascript-typescript`) | `195fe1a` | success |
| [41593030523](https://github.com/rachitst/pbip/actions/runs/41593030523) | Performance (k6 + page load), the latest green run | `b0f47fb` | success |
| [41602814425](https://github.com/rachitst/pbip/actions/runs/41602814425), [41600683806](https://github.com/rachitst/pbip/actions/runs/41600683806) | Performance, the latest runs | `195fe1a` | **failure** (§5.2) |
| [41415493200](https://github.com/rachitst/pbip/actions/runs/41415493200) | Fuzz (nightly Schemathesis), the latest run | `0c7607a` | success |

Run 41600683787 is the latest `main` run where every CI job passed. Unless a row says otherwise, the **[CI]** numbers
below come from that run.

## 2. Test distribution (acceptance criterion 1)

### 2.1 Counts per suite **[CI]**

The API job runs one `pytest --cov` over `apps/api/tests`, `ml/tests` and `data-gen/tests` (`pyproject.toml`
`testpaths`). It collected 1,959 items: **1,949 passed, 10 skipped, 0 failed** (job "API - unit, integration,
security tests", 14 min 56 s). The split by directory below was counted from the per-file progress lines in the
same log.

| Suite | Where | Passed | Skipped | Source |
|---|---|---:|---:|---|
| API unit | `apps/api/tests/unit` | 851 | 10 | run 41600683787, API job |
| API integration (real PostgreSQL 16 + Redis) | `apps/api/tests/integration` | 798 | 0 | run 41600683787, API job |
| API security | `apps/api/tests/security` | 15 | 0 | run 41600683787, API job |
| ML (`pbip_ml`) | `ml/tests` | 225 | 0 | run 41600683787, API job |
| Data generator | `data-gen/tests` | 60 | 0 | run 41600683787, API job |
| ML + data-gen again, with no database allowed | `ml/tests data-gen/tests` | 285 | 0 | run 41600683787, ML job |
| Web (Vitest + Testing Library) | `apps/web/src` | 375 (43 files) | 0 | run 41600683787, Web job |
| E2E (Playwright, Chromium, full Compose stack over TLS) | `apps/web/e2e` | 21 (33.9 s, 2 workers) | 0 | run 41600683787, E2E job |

The 10 skips are expected:

* 8 in `test_dev_runner.py` run the real `cmd.exe` or Windows PowerShell, so they skip on Linux.
* 2 in `test_geoip.py` and `test_fraud_travel.py` need a GeoIP MMDB file (`PBIP_GEOIP_TEST_DB`,
  `PBIP_GEOIP_CITY_TEST_DB`), which CI does not have.

**Security tests are wider than the `security/` directory.** That directory holds two cross-cutting files:
`test_route_policy_registry.py`, which fails the build for any route without authentication that is not on the
allow-list, and `test_security_headers.py`. The `security` pytest marker is also used by 19 integration files (for
example `test_rbac_bola.py` with 13 tests, `test_assistant_adversarial.py`, `test_auth.py`) and 1 unit file. That
gives 22 files in total, counted from the source. CI does not report counts per marker, so **[Not measured]**: the
number of tests that carry the `security` marker.

### 2.2 Shape of the pyramid **[CI]**

The blueprint target is about 70% unit, about 25% API/integration and under 5% E2E. Using the 2,345 tests that
passed in run 41600683787 (the ML/data-gen job's second run is not counted again):

| Layer | Tests | Share |
|---|---:|---:|
| Unit (API unit 851, ML 225, data-gen 60, web 375) | 1,511 | 64.4% |
| Integration and security against PostgreSQL (798 + 15) | 813 | 34.7% |
| E2E | 21 | 0.9% |

The integration layer is larger than the target, by design. Every ledger, authorization and concurrency guarantee
is tested against a real PostgreSQL (AGENTS.md §6 forbids SQLite), so the tests that matter most are integration
tests.

### 2.3 E2E journeys

The 21 Playwright tests that passed in run 41600683787 include the critical journeys listed in
[`e2e.md`](e2e.md) (TASK-077, #319). Those journeys are: onboarding with MFA enrolment; a transfer with its
idempotent replay; step-up for a large transfer; an analyst releasing or rejecting a held transfer; a standing
order; the assistant answering a balance question; BOLA (another customer's ids give 404); administration
(TASK-108, #359); and the product comparison layout (#360). [`e2e.md`](e2e.md) also has local runs **[Laptop-U]**
(16 passed, three times in a row, 2 Nov 2026). Those runs used an earlier version of the suite.

## 3. Coverage (acceptance criterion 2)

### 3.1 Python **[CI]**

The figures come from the job log (`term-missing`) and the `coverage-xml` artifact (id 13214434506) of run
41600683787. Branch coverage is on (`[tool.coverage.run] branch = true`).

| Scope | Lines | Branches | Combined (coverage.py "Cover") |
|---|---|---|---|
| **All Python** (gate 80%) | 12,040 / 12,338 = 97.58% | 1,872 / 2,064 = 90.70% | **96.60%**: "Required test coverage of 80.0% reached" |
| `modules/ledger` | 279 / 280 = 99.64% | 51 / 52 = **98.08%** | 99.40% |
| `modules/auth` | 637 / 653 = 97.55% | 86 / 96 = **89.58%** | 96.53% |
| `modules/payments` | 868 / 901 = 96.34% | 174 / 200 = 87.00% | 94.64% |
| `core/security.py` (JWT, hashing) | 69 / 69 = 100% | 12 / 12 = 100% | 100% |

CI prints coverage per file only, not per module. The module rows above were made by adding up the per-file
line and branch counts in the CI `coverage.xml`. No tests were run locally for them, and the per-file figures
agree with the job log (`auth/service.py` 95%, `ledger/service.py` 99%, `payments/service.py` 96%). Where the
branches are missed:

* auth: `auth/service.py` 75/84 (missed lines 191, 240-242, 289->291, 296, 400, 453, 456), `auth/router.py` 3/4.
* payments: `scheduled_service.py` 36/48, `payments/service.py` 108/120, `recurrence.py` 20/22.

### 3.2 Web **[CI]**

`npm run test:coverage`, "All files": statements 92.24%, branches 86.39%, functions 90.99%, lines 93.53% (run
41600683787, Web job).

### 3.3 Mutation score (stronger than coverage)

[`mutation-report.md`](mutation-report.md) (TASK-086, #297): **1,041 of 1,076 mutants killed = 96.7%** on
`ledger/service.py` (97.6%) and `payments/service.py` (96.4%). No critical mutant survives, and the target was
≥ 85%. **[Laptop-U]**, base commit `cdda170`. The run is a documented command, not a CI job, because a full run
takes about 3 hours on that laptop.

## 4. Concurrency and deadlock prevention (acceptance criterion 3)

### 4.1 Why there are no deadlocks (design)

The lock order in [`AGENTS.md` §4](../../AGENTS.md) applies to every money-moving request:

1. the per-user advisory lock (`_lock_user`, `payments/service.py:418`, "ALWAYS the first lock taken in a request");
2. the idempotency key insert;
3. the accounts, locked `FOR UPDATE` in **sorted id order** by `ledger.lock_accounts`
   (`ledger/service.py:88-90`: `sorted(set(account_ids))` and `ORDER BY id`);
4. only then the transaction header insert.

A deadlock needs a cycle, where two transactions each hold a lock the other is waiting for. Each request takes
only its own user's advisory lock, and it takes it first. All account row locks are then taken in one global
order, so two transfers between the same accounts in opposite directions queue on the lower id and cannot cross.
The header goes last because its FK inserts take `KEY SHARE` locks on the account rows. Locking the accounts after
those inserts caused a lock-upgrade deadlock that `test_concurrent_spending_never_overdraws` found while the
ledger was being built (AGENTS.md §4, blueprint "Bugs found by the tests"). This is a design argument checked by
tests and load runs. **It is not a machine-checked proof.**

### 4.2 Concurrency tests **[CI]**

All of these use real threads against PostgreSQL. All of them passed in run 41600683787, because all 798
integration tests passed.

| Guarantee | Test | What it does |
|---|---|---|
| No overdraft | `test_ledger.py::test_concurrent_spending_never_overdraws` | 14 threads behind a barrier each spend 100 from an account holding 1,000. Exactly 10 succeed; the balances end at 0 / 1,000. |
| No deadlock | `test_ledger.py::test_opposing_concurrent_transfers_do_not_deadlock_and_conserve_money` | 12 threads move money A→B and B→A at the same time. All 12 succeed and the total stays 20,000. |
| Money conserved | `test_ledger.py::test_global_ledger_always_sums_to_zero` | 8 concurrent transfers. The sum of entries is 0 and the sum of balances is 0. |
| Exactly-once idempotency | `test_transfers.py::test_concurrent_duplicates_create_exactly_one_transfer` | 10 threads send the same `Idempotency-Key` over HTTP. All get 201 with the same id, exactly one is a real execution and there is one `TRANSFER` row. |

The same pattern covers the other money and state paths. Each of these passed in the same run:

* bills: duplicate key, and competing payments that never overdraw;
* loans: duplicate applications, stacked loans, repayment runs;
* wealth: duplicate bookings, bookings racing transfers, closures;
* FD maturity payouts;
* scheduled transfers: one key, workers sharing one due occurrence, advisory-lock contention;
* fraud: concurrent clears and mule inbound totals;
* RM assignment, runtime configuration changes, compliance acknowledgements, device registration, forecast
  alerts, and the velocity counter (unit).

### 4.3 Load-level outcomes

After every k6 run, `scripts/load_invariants.py` checks the database for deadlocks, overdrawn accounts,
unbalanced transactions, balance/entry mismatches and money conservation:

| Where | Load | Outcome |
|---|---|---|
| **[CI]** Performance runs on `main`, 3 Nov 2026 (41561633608, 41565201783, 41567122534, 41591459129, 41593030523, 41598754434, 41600683806, 41602814425) | 50 VUs, 2 min, 100 users, through nginx TLS | **0 deadlocks and `violations: []` in all 8 runs**. In run 41593030523: 1,324 transfers, money conserved (10,000,000,000 minor units), 0 HTTP errors, 14,242 of 14,242 checks passed. |
| **[Laptop-U]** [`performance-2026-11-01.md`](performance-2026-11-01.md) (TASK-078) | 50 VUs, 3 min steady, 200 users, load average 9-27 | 0 deadlocks, 0 overdrawn, 0 unbalanced, money conserved. 2,199 transfers in total across the runs, and every idempotent replay returned the same transfer. |

**Verdict for criterion 3:** met. The concurrency outcomes come from the tests and the load runs, and the
deadlock prevention rests on the lock-order design in §4.1, checked by the opposing-transfer test and by 0
deadlocks in every load run.

## 5. Performance

### 5.1 Latest green run **[CI]**, run 41593030523

Runner: GitHub-hosted Linux, 4 vCPU (AMD EPYC 9V45), 15 GiB RAM (`environment.txt` in the `perf-results`
artifact).

| k6 metric | p95 | Threshold |
|---|---|---|
| accounts | 34.34 ms | < 500 ms |
| analytics | 43.59 ms | < 500 ms |
| assistant | 87.47 ms | < 500 ms |
| transfers | 138.82 ms | < 500 ms |
| login | 213.74 ms | < 3,000 ms |
| refresh | 65.97 ms | < 500 ms |
| all requests | 86.36 ms (14,442 requests, 0.00% failed) | |

Page load in the same run (Playwright `perf.spec.ts`, one user): `/` 154 ms, login 133 ms, `/accounts` 119 ms,
`/insights` 368 ms, `/profile` 99 ms. The budgets are 2 s per page and 3 s for login.

### 5.2 The Performance job is red on the latest `main` commits **[CI]**

The Performance workflow is not part of `CI Gate` (it runs nightly and on pushes to `main`). Five of the 8 runs on
3 Nov failed, all on the transfers p95 threshold, and run 41598754434 also failed on assistant (603.1 ms):

| Run | Runner CPU | transfers p95 |
|---|---|---|
| 41602814425 (schedule, `195fe1a`) | EPYC 7763 | 759.76 ms ✗ |
| 41600683806 (push, `195fe1a`) | EPYC 7763 | 738.82 ms ✗ |
| 41598754434 (push, `50f3a24`) | EPYC 7763 | 923.7 ms ✗ |
| 41591459129 (push, `fe7754b`) | EPYC 7763 | 603.01 ms ✗ |
| 41565201783 (push, `cf5847e`) | EPYC 7763 | 654.09 ms ✗ |
| 41593030523 (push, `b0f47fb`) | EPYC 9V45 | 138.82 ms ✓ |
| 41567122534 (push, `f4149c0`) | EPYC 9V74 | 261.93 ms ✓ |
| 41561633608 (push, `7a594a4`) | EPYC 9V45 | 175.34 ms ✓ |

Every failure ran on an EPYC 7763 runner and every pass ran on a 9V45 or 9V74 runner. The pass or fail result
follows the runner hardware, not the commit. Even in the failing runs, HTTP errors were 0.00% and there were 0
invariant violations. This report does not diagnose the cause. **The 500 ms transfers budget is not met
consistently on GitHub-hosted runners**, so this needs a follow-up issue.

### 5.3 Laptops

* **[Laptop-U]** [`performance-2026-11-01.md`](performance-2026-11-01.md): with everything running on the
  2-core Celeron at 50 VUs, the p95 is 1.5-5 s for every route group. That is CPU starvation, not a capacity
  figure, and the document says so. Page load: `/` 1,665 ms, login 2,102 ms, `/accounts` 942 ms, `/insights`
  1,288 ms, `/profile` 913 ms (all within budget).
* **[Laptop-W]** #329 [results comment](https://github.com/rachitst/pbip/issues/329#issuecomment-6598676641)
  (3 Nov 2026), checklist step 7: `.\dev.ps1 perf` gave p95 overall / live / ready of 16.70 / 16.19 / 17.69 ms,
  and `cmd /c dev.cmd perf` gave 15.77 / 14.52 / 16.60 ms, at 50 VUs with 0.00% errors. This is the k6 smoke
  profile against the health endpoints, not the full load mix in §5.1.
* **[Not measured]** Any number on staging hardware, and 500 VUs (#198, #101).

## 6. Security testing

| Check | Status | Source |
|---|---|---|
| CodeQL (Python, JavaScript/TypeScript) | both analyses succeeded; 0 open code-scanning alerts (API query, 3 Nov 2026) | **[CI]** run 41600683862 |
| Bandit (`-ll`, medium+ severity) | no findings, step passed | **[CI]** run 41600683787, Security job |
| pip-audit | "No known vulnerabilities found" | **[CI]** run 41600683787, Security job |
| npm audit (`--audit-level=high`) | passed with **13 moderate** advisories: `minimatch` ReDoS (GHSA-g3hv-75wg-m249), pulled in by ESLint / `@typescript-eslint` and `openapi-typescript` (build tooling, not shipped). 0 high or critical. The same advisory is open as Dependabot alert #3 (`apps/web/package-lock.json`, moderate). | **[CI]** run 41600683787, Security job; Dependabot |
| detect-secrets against the baseline | passed | **[CI]** run 41600683787, Security job |
| API fuzzing (Schemathesis, TASK-070) | 59 operations, 562 cases in positive and negative mode, **0 unhandled 5xx, 0 schema violations** | **[CI]** run 41415493200 (2 Nov 2026), [`fuzzing.md`](fuzzing.md) |
| OWASP ZAP baseline (DAST) | latest run succeeded | **[CI]** run 41421122439 (2 Nov 2026), [`../security/zap-2026-11-01`](../security/zap-2026-11-01) |
| BOLA matrix, route-policy registry, adversarial assistant suite | passed | **[CI]** run 41600683787 (§2.1) |

## 7. ML and fraud

* **[CI]** The 225 ML tests (contracts, determinism, behaviour regression, metric gate) and 60 data-gen tests
  passed, in both the API job and the ML job, which runs without a database (§2.1).
* The four hybrid-model bars, from [`model-evaluation-report-v1.md` §8](../ml/model-evaluation-report-v1.md)
  (generator v2.2, MLflow run `394b64764f694164b3a1e0d356f4cf1d`, synthetic data only). Results are test /
  held-out, and all four pass:

  | Bar | Result |
  |---|---|
  | Legitimate new-payee transfers held for REVIEW ≤ 1% | 0.09% / 0.30% |
  | Legitimate new-payee transfers asked for STEP_UP ≤ 10% | 7.5% / 8.0% |
  | All legitimate debits held at REVIEW ≤ 0.1% | 0.02% / 0.04% |
  | Recall at REVIEW, hybrid clearly better than rules alone | 0.808 vs 0.202 |

  The report does not state the machine it was trained on, and it says latency was not re-measured for §8. The
  Performance job retrains on each run as part of its stack setup. In run 41593030523 **[CI]** the LightGBM
  test-split result was precision 0.9029, recall 0.8942, PR-AUC 0.9615, 10 false positives and 11 false
  negatives in 10,258 rows, which matches the report's "P 0.903, R 0.894, PR-AUC 0.962".
* The evaluation is on synthetic data from the project's own generator. It shows that the models separate the
  planted patterns. It is not an estimate of real-world fraud performance (the report's own caveat).

## 8. Operations: backup and restore drill

[`runbook.md` §4.2](../operations/runbook.md) (TASK-083, #293), **[Laptop-U]**, 1 Nov 2026, local stack (no
staging host exists):

* Both encryption modes, symmetric AES-256 and public key, gave **PASS**. 39 tables and 775,226 rows were restored
  identical to the manifest. The ledger reconciled (net 0, 0 unbalanced) and the audit chain verified 151 of 151
  rows.
* Three negative runs (a flipped byte, the wrong passphrase, and a tampered byte with the manifest rewritten to
  match) all gave **FAIL** with exit 1, and no drill database was left behind.
* `test_backup_restore.py` (2 integration tests) passed in **[CI]** run 41600683787.
* **[Not measured]** A drill on staging, off-host storage, and WAL point-in-time recovery (marked External in the
  runbook).

## 9. Windows verification **[Laptop-W]**

From the #329 results comment (3 Nov 2026, `main` at `b0f47fb`) and #371:

| Item | Result |
|---|---|
| SETUP_GUIDE §4.7 checklist | **8 of 9 steps pass.** Step 4 fails: `migrate` because the compose volume is shared between two checkouts (real, not Windows-specific), and `test-unit` (below). |
| `.\dev.ps1 test-unit` from PowerShell | **10 failed, 1,003 passed, 5 skipped** (exit 1). In a fresh clone: 11 failed, 1,002 passed. |
| `.\dev.ps1 test-web` | **375 passed** (43 files), exit 0 |
| The #352 test files from Git Bash (the pre-push hook context) | 54 passed, 2 skipped, 0 failed |

The causes, from #371:

* 9 × `test_deploy_script.py`: from PowerShell, `shutil.which("bash")` finds the WSL launcher
  `C:\Windows\System32\bash.exe`. The cause is the Windows test environment.
* `test_backup_restore.py::test_run_pipeline_reports_both_exit_codes`: `printf` and `cat` are not on a plain
  Windows PATH. The cause is the Windows test environment.
* Fresh clone only, `test_fraud_travel.py::test_travel_defaults_and_policy_from_settings`: an empty
  `GEOIP_CITY_DB_PATH=` in `.env` leaks into the test environment. This is real and not Windows-specific.

#371 is open. The same tests pass on Linux in CI.

## 10. Acceptance criteria (TASK-087)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Details test distribution: unit, integration, security tests and E2E journeys | **Met** | §2: CI counts per suite (851 / 798 / 15 / 225 / 60 / 375 / 21), the pyramid shares and the E2E journeys. The one gap is that CI does not report counts per `security` marker. |
| 2 | Reports branch coverage across core modules (ledger ≥ 90%, auth ≥ 90%) | **Met (after #410)** | §3.1. Branch coverage is reported per module. Ledger branches are 98.08%: **met**. Auth branches were **89.58% (86/96): below 90%** in run 41600683787. **Update (3 Nov):** #410 added 5 auth edge-case tests; in CI run 41689113122 (PR #407, merged as `fc4279a`) auth branches are **92/96 = 95.83%** and ledger branches 54/56 = 96.43% (total 96.71%), counted from that run's `coverage-xml` artifact the same way as §3.1. Auth lines (97.55%) and combined coverage (96.53%) are above 90%, but the criterion asks for branch coverage. |
| 3 | Documents concurrency stress test outcomes and deadlock prevention proof | **Met** | §4: the lock-order design argument, 4 core concurrency tests and more than 20 others passing in CI, and 0 deadlocks and 0 invariant violations in 8 CI load runs and the laptop runs. The "proof" is a design argument checked by tests, not a formal proof. |

**Definition of done:** this file is at the path the backlog names (`docs/testing/test-strategy-and-report.md`).
Peer review is pending on the PR.

## 11. Known gaps and follow-ups

* ~~Auth branch coverage is 89.58%, 1 branch short of 90%.~~ Fixed by #410: 95.83% in CI run 41689113122.
* The Performance job is red on `main`, depending on the runner hardware (§5.2).
* `npm audit` reports 13 moderate advisories in build tooling (§6).
* Windows-only test-environment failures (#371).
* **[Not measured]** on staging: load, page load, restore drill, and runtime GeoIP / beat verification (#198, #101).
