# ADR 0021: Access tokens are checked against the user's status and token version on every request
- Status: Accepted
- Date: 2026-11-01
- Deciders: Architecture Team (security-model change, AGENTS.md rule 5)

## Context
Access tokens are HS256 JWTs valid for `JWT_ACCESS_TTL_MINUTES` (15). Until now `get_current_principal`
only verified the signature and claims, so a token stayed valid for up to 15 minutes after the account was
disabled (runbook §5.2) or the password was changed. The threat model listed this as a known gap with an
access-token denylist as an open COULD item; the incident runbook's only immediate remedy was rotating
`JWT_SECRET`, which logs out every user.

Options considered:
1. **Denylist of `jti` in Redis** (with a PostgreSQL fallback): needs every issued `jti` per user, or a
   per-user "revoked before" marker; Redis and PostgreSQL can disagree, and a Redis outage must fail closed
   (rejecting every request) or open (the gap returns).
2. **Shorter TTL** (for example 2 minutes): narrows the window but does not close it, and multiplies refresh
   traffic.
3. **Token version in PostgreSQL, checked per request** (chosen): one primary-key lookup on the session the
   request already uses; strongly consistent with the transaction that disabled the user.

## Decision
- `users.token_version INTEGER NOT NULL DEFAULT 0` (CHECK `>= 0`, migration 0011).
- Access tokens carry it as the `ver` claim (`core/security.encode_token`); a token without the claim (issued
  before this change) is version 0, so the upgrade logs nobody out.
- `get_current_principal` calls `auth.service.access_token_is_current(session, user_id, ver)`: the token is
  accepted only while the user exists, is `ACTIVE` and `ver == users.token_version`. Otherwise 401 with the same
  body as a forged or expired token (no account-state oracle).
- `AuthService._revoke_all_user_sessions` (password change) increments the version in the same transaction
  that revokes the refresh tokens.
- Disabling a user (`UPDATE users SET status = 'DISABLED'`, runbook §5.2) takes effect on the next request with
  no version bump needed.

## Consequences
- **Closes the gap**: disable and password change end every outstanding access token immediately. Proven by
  `tests/integration/test_access_token_revocation.py` (disable, re-enable, password change on two sessions,
  legacy token, unknown user, 16 concurrent requests after a disable) and `tests/unit/test_token_version_claim.py`.
- **Cost**: one indexed lookup per authenticated request. Measured on the reference laptop (2-CPU Celeron,
  shared PostgreSQL, load average ~6.5): median 1.9 ms, p95 4.2 ms, p99 7.4 ms over 3,000 calls. FastAPI caches
  `get_session` per request, so the check reuses the endpoint's session and connection.
- **Availability**: if PostgreSQL is down, authenticated requests fail with it, which they did before for every
  route that reads data. Health endpoints are unauthenticated and unaffected.
- **Not covered**: logout revokes only its own refresh-token family; the access token used to log out stays
  valid until it expires (an attacker who holds it is not stopped by the victim's logout). Changing the
  password, or disabling the account, ends it. A per-session (`jti`) denylist would close that remaining case
  and is left as the open COULD item in the threat model.
