# Security audit, 2026-11-01 (TASK-090)

Pre-ship verification: static analysis, dependency audit, secret scan of the whole history, an OWASP ZAP
baseline scan of the running stack over HTTPS, and the audit hash-chain check. Every number below was measured;
where it was measured is stated. **Machine:** the reference laptop (2-CPU Intel Celeron, 7 GB RAM, Linux), shared
with other work, so durations are indicative only. **Not staging**: no staging host exists yet (#198).

**Code under test:** branch `feat/task-090-security-audit` on top of `feat/tls-termination` (#280), which includes
`main` at `f816bfe`. The stack ran as a separate compose project (`pbiptls`, ports 18080/18443) built from that
code, with HTTPS through nginx and TLS to PostgreSQL.

## Results against the acceptance criteria

| Criterion | Tool and scope | Result | Status |
|---|---|---|---|
| Bandit: zero high/medium | Bandit 1.9.4, `apps/api/src`, `ml/src`, `data-gen/src` (14,485 lines), project config | **0 high, 0 medium**, 11 low (6 × B105 and 2 × B106 "hardcoded password" on constant names such as `token_type="access"`, 3 × B101 `assert` in offline ML code) | Met |
| pip-audit and npm audit: zero high/critical | pip-audit 2.10.1 over the locked export (631 lines, all workspace packages); `npm audit` in `apps/web` | Python: **no known vulnerabilities**. npm production dependencies (`--omit=dev`): **0**. npm including dev tooling: **13 moderate, 0 high, 0 critical** (accepted, A-1) | Met |
| No secrets or private keys in history | gitleaks 8.28.0 over **all refs** (190 commits, 6.3 MB); detect-secrets (pre-commit and CI) on every commit | 16 gitleaks findings, **all false positives** (A-2); detect-secrets baseline clean | Met |
| Audit chain verification proves 100% integrity | `GET /api/v1/compliance/audit-logs/verify` as a compliance officer with an MFA session, over HTTPS | `{"valid": true, "checked": 12, "first_invalid_seq": null}` on the freshly seeded stack | Met (small chain; re-checked after the load test, see TASK-078 report) |
| OWASP verification (user request: ZAP baseline against the running stack) | OWASP ZAP 2.16.1 `zap-baseline.py`, target `https://localhost:18443`, 3-minute spider, passive rules | Before the fix: 0 FAIL, **6 WARN**, 61 PASS. After: **0 FAIL, 0 WARN**, 5 IGNORE (accepted, A-3), 62 PASS, exit 0 | Met |

`make security` (Bandit, pip-audit, npm audit for high/critical) exited 0.

## Findings fixed

| ID | Finding | Fix | Proof |
|---|---|---|---|
| F-1 | ZAP 90004 *Insufficient Site Isolation Against Spectre* (Low, 8 instances): static responses had no `Cross-Origin-Resource-Policy` or `Cross-Origin-Embedder-Policy` (the API already sent CORP) | `infra/nginx/security-headers.conf` adds `Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp`. The SPA loads only same-origin resources (CSP `default-src 'self'`), so nothing breaks | `test_nginx_security_headers.py`; ZAP re-run: 90004 gone; the Playwright E2E specs pass against the rebuilt image |
| F-2 | Found while verifying TLS (#280): on the HTTPS stack the CSRF guard rejected every refresh (403), because `CORS_ORIGINS` listed only `http://localhost:8080`. Users would be signed out on every reload | Fixed in #280: the compose stack always trusts its HTTPS origin; `scripts/verify_tls.sh` asserts a refresh from that origin is 401, never 403 | `verify_tls.sh` failed before the fix and passes after it, on the running stack |
| F-3 | Threat-model known gap: a disabled user's access token stayed valid up to 15 minutes | Per-request status and token-version check (ADR-0021, #281) | `test_access_token_revocation.py` (5 of its 6 tests fail without the change) |

## Accepted findings (with reasons)

- **A-1, npm dev tooling (13 moderate).** All 13 are one advisory chain: `minimatch` ReDoS reached through `eslint`,
  `typescript-eslint`, `@redocly/openapi-core` and `openapi-typescript`. They run only on developer machines and in
  CI on repository-controlled input. None ships: production uses only the static build behind nginx (AGENTS.md:
  Node is build tooling only), and `npm audit --omit=dev` reports 0. The fix is a major-version bump of the
  lint stack, and AGENTS.md rule 6 keeps dependency bumps out of feature PRs. Dependabot (`.github/dependabot.yml`)
  proposes it; review it before 2026-12-01.
- **A-2, gitleaks false positives (16).** `scripts/smoke_test.py` and `scripts/smoke_test.sh` send a deliberately
  wrong password (`definitely-wrong-123`) to prove bad credentials are rejected. 13 hits are `hashed_secret` entries
  in `.secrets.baseline`: SHA-1 fingerprints detect-secrets stores so it can recognise audited values, not secrets.
  `tests/unit/test_logging_redaction.py` has a synthetic JWT (`{"sub":"1234"}`, signature `signature123`) that tests
  log redaction. No real credential, key or token was found in any commit on any ref.
- **A-3, ZAP informational and tool notices (5 rules, IGNORE in `infra/zap/baseline-rules.tsv`).** 10116 *ZAP is out
  of date*: the image is pinned on purpose. 10027 *Suspicious comments*: the word "select" inside the minified React
  bundle; no source comments ship. 10109 *Modern web application*: informational. 10015 *Re-examine Cache-control*
  and 10049 *Storable content*: `index.html` is `no-cache` so a deploy is picked up immediately, and hashed
  `/assets/*` are `immutable` by design; neither contains personal data, and API responses are not involved.

## Raw reports

`docs/security/zap-2026-11-01/`: `before.json` and `before.md` (6 WARN, before the header fix), `after.json` and
`after.html` (0 WARN/0 FAIL with `infra/zap/baseline-rules.tsv`). Target `https://localhost:18443` on the laptop.

## Made repeatable

- `.github/workflows/dast.yml`: nightly and on demand, it builds the full stack with dev certificates and runs the
  same ZAP baseline with `infra/zap/baseline-rules.tsv`. Any new warning fails the job, and the HTML, JSON and
  Markdown reports are uploaded as an artifact.
- Existing gates unchanged: Bandit, pip-audit, npm audit and detect-secrets in the CI `Security` job; CodeQL;
  nightly Schemathesis fuzzing of the API (`fuzz.yml`).

## Limits of this audit

- The ZAP *baseline* is passive. It sees what the spider reaches: the SPA shell, static assets and unauthenticated
  API responses. Authenticated API behaviour is covered by the RBAC/BOLA matrix, the adversarial assistant suite
  and Schemathesis fuzzing, not by ZAP. An authenticated active scan (`zap-api-scan.py` with a token) was not run.
- The gitleaks scan covered the refs present in the local clone on 2026-11-01. Forks and deleted remote branches
  were not scanned.
- The audit chain checked 12 events on a fresh stack. It is a correctness proof of the verifier against real
  writes, not a statement about any long-lived environment.
