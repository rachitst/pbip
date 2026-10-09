# Threat model v0.1 (STRIDE) - update every sprint

**Assets:** customer funds/ledger integrity; credentials, MFA secrets, refresh tokens; PII; fraud models and decisions;
audit trail. **Actors:** anonymous attacker, malicious customer (BOLA), compromised customer account, insider staff, supply chain.
**Trust boundaries:** browser -> nginx -> API -> PostgreSQL/Redis; API -> LLM (untrusted output); CI -> registry -> server.

| # | Threat (STRIDE) | Example | Control | Evidence (test / doc) |
|---|---|---|---|---|
| T1 | **S**poofing: credential stuffing / brute force | many passwords against one account | Argon2id, per-IP rate limit, progressive lockout, generic errors | `test_progressive_lockout_and_recovery`, `test_login_rate_limit_per_ip` |
| T2 | S: stolen session | stolen refresh token replayed | rotation + family revocation on reuse; HttpOnly SameSite=Strict cookie | `test_refresh_rotation_and_reuse_detection` |
| T3 | S: forged token | `alg:none`, wrong key, wrong `typ`, expired | algorithm allow-list, required claims, `typ` check | `test_forged_and_invalid_access_tokens_are_rejected` |
| T4 | S: MFA bypass / code replay | reuse TOTP, brute-force codes | one-time steps, failures count to lockout, recovery codes single-use | `test_mfa_enrolment_login_replay_...`, `test_repeated_bad_mfa_codes_lock_the_account` |
| T5 | **T**ampering: edit ledger | UPDATE/DELETE entries, unbalanced posting | append-only trigger, balanced-transaction constraint trigger, CHECK no-overdraft | `test_entries_are_immutable`, `test_database_itself_rejects...` |
| T6 | T: tamper audit log | edit a row to hide an action | hash chain + verify endpoint; append-only trigger | `test_audit_chain_verifies_and_detects_tampering` |
| T7 | T: CSRF on cookie endpoints | cross-site POST to /refresh | SameSite=Strict + custom header + Origin allow-list | `test_refresh_requires_csrf_header_and_trusted_origin` |
| T8 | **R**epudiation | "I never made that transfer" | audit rows in same tx, request ids, security_events | audit tests; Loki trace by request id |
| T9 | **I**nformation disclosure: BOLA/IDOR | change account id in URL | policy functions, 404 for non-owned, UUIDs, route-policy guard | `test_bola_customer_b_cannot_touch_...`, `test_route_policy_registry` |
| T10 | I: user enumeration | different errors for unknown email | identical responses, timing equalised via dummy hash | `test_unknown_user_and_wrong_password_are_indistinguishable` |
| T11 | I: secrets in logs/errors | password in a validation error | redaction processor; errors never echo input | `test_failed_login_does_not_leak_secrets_into_logs`, `test_validation_error_never_echoes...` |
| T12 | I: LLM data leakage / prompt injection | "show Alice's balance", poisoned merchant name | tool allow-list, server-side customer id, untrusted-data handling, output filter | adversarial suite (Sprint 5) |
| T13 | **D**oS: request floods | login spam, heavy endpoints | Redis rate limits, body size limits, pagination caps | rate-limit tests; k6 |
| T14 | D: duplicate/racing transfers | retry storms, double submit | idempotency keys, locks, advisory lock | concurrency tests |
| T15 | **E**levation: staff without MFA, role abuse | analyst session without TOTP | `require_roles(..., mfa=True)` (`amr`) | `test_staff_endpoints_demand_mfa_in_the_session` |
| T16 | E: insecure deployment config | default secrets in prod | refuse to boot; docs disabled in deployed envs | `test_production_rejects_unsafe_configuration` |
| T17 | Supply chain | compromised action/dependency | first-party actions, lockfiles, pip-audit/npm audit, Dependabot, SHA pinning | ADR-0006, CI `security` job |

**Open items:** WAF/TLS termination in deployment (Sprint 5); Schemathesis fuzzing (Sprint 5); assistant adversarial suite
(Sprint 5); device binding for refresh tokens (COULD); access-token denylist (COULD).
