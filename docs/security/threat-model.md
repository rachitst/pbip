# Threat model v0.2 (STRIDE) - update every sprint

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

**Sprint 1 additions**

| # | Threat (STRIDE) | Example | Control | Evidence (test / doc) |
|---|---|---|---|---|
| T18 | S: password compromise outlives the change | attacker keeps a refresh token after the victim changes password | password change revokes every refresh-token family; Argon2id check of the current password; failures count toward lockout; rate limit. Access JWTs live <= 15 min (documented limit) | `test_password_change_revokes_all_sessions_across_families`, `test_password_change_wrong_current_password_and_lockout` |
| T19 | S: weak/breached new password | `Summer2025!`, leetspeak of common roots | offline blocklist + structural checks (no network egress of passwords) | `test_passwords.py`, `test_password_change_rejects_policy_failures` |
| T20 | E: lockout bypass via admin unlock | non-admin or session without TOTP unlocks an account | `require_roles(ADMIN, mfa=True)`; unlock is audited (admin + target ids) and emits ACCOUNT_UNLOCKED; it clears per-IP limiter windows only for IPs that failed against this user alone, so a credential-spraying IP keeps its limit | `test_only_mfa_verified_admins_may_inspect_or_unlock`, `test_unlock_is_audited_with_admin_and_target_ids`, `test_unlock_does_not_reset_the_limiter_of_an_ip_spraying_other_accounts` |
| T21 | I: cursor tampering on transaction history | crafted cursor to read another account's entries | ownership checked before the cursor is decoded (404); cursor only narrows an owned query; malformed cursor = 422 without echo | `test_ownership_is_checked_before_any_cursor_is_used`, `test_malformed_cursors_are_a_422_not_a_500` |
| T22 | D: probe amplification | flood `/api/health/ready` to drive DB/Redis load | readiness result cached for 1 s, single-flight refresh | `test_readiness_is_cached_for_the_ttl_then_rechecked`; k6 baseline |
| T23 | D/T: device-registration race | concurrent first registrations -> 500s | atomic upsert on (customer, fingerprint hash); raw fingerprints never stored or echoed; header length bounded | `test_concurrent_first_registration_of_one_device_never_fails`, `test_oversized_device_header_is_rejected` |
| T24 | T: synthetic data contaminates the ledger | bulk load writes fake customers/entries into operational tables | separate `synthetic` schema; loader never touches `public`; DB triggers reject orphan/cross-run rows and edits (ADR 0009) | `test_copy_loads_1000_customers_in_under_15_seconds` (ledger untouched), trigger tests |

**Open items:** WAF/TLS termination in deployment (Sprint 5); Schemathesis fuzzing (Sprint 5); assistant adversarial suite
(Sprint 5); device binding for refresh tokens (COULD); access-token denylist (COULD).
