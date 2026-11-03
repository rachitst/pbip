# ADR 0031: Runtime configuration of an allow-listed set of business limits
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
TASK-108 (#282, FR-05) asks for an administration screen where an ADMIN can view and change "the application settings
that are safe to change at runtime", for example the daily transfer limit, with typed, bounded validation, an audit
row per change (who, old value, new value, reason), and no way to change secrets or security settings from the UI.

Today every setting is read once from the environment into `core/config.Settings` (`get_settings()` is
`lru_cache`d) and is fixed for the life of a process. Changing the daily transfer limit means editing the deployment
and restarting the API and the Celery worker. A value changed at runtime must therefore live somewhere every process
can see, and every reader must use it.

## Decision
1. **Storage: one small table, `runtime_settings`** (migration after 0019): `id` UUID PK, `key` (unique, CHECK in
   the allow-list), `value` `BIGINT` (CHECK `value >= 0`), `updated_by_user_id` -> `users(id)` `ON DELETE RESTRICT`,
   `created_at`, `updated_at`. A row exists only while a setting is overridden; no row means the environment value
   applies. The table holds the *current* override only; the history (who, old, new, reason) is the hash-chained
   audit log, which is append-only and already verified, so there is no second history table to keep in step.

2. **The allow-list is code, not data** (`modules/runtime_config/allowlist.py`). Each entry names the `Settings`
   field, its unit and its runtime bounds. Only these three business limits are allow-listed:

   | Key (`Settings` field) | Unit | Min | Max | Env default |
   |---|---|---|---|---|
   | `daily_transfer_limit_minor` | minor units | 10,000 (100.00) | 1,000,000,000 (1,00,00,000.00) | 50,000,000 |
   | `max_transfer_minor` | minor units | 10,000 (100.00) | 1,000,000,000 (1,00,00,000.00) | 20,000,000 |
   | `max_active_scheduled_transfers_per_customer` | count | 1 | 200 | 20 |

   Everything else is excluded by construction (the key is an enum and a DB CHECK): secrets (`jwt_secret`,
   `field_encryption_key`), authentication and session settings (TTLs, Argon2 cost, lockout, rate limits, MFA),
   network and URL settings (`cors_origins`, `ollama_base_url`, `database_url`), the fraud scorer selection and model
   paths, and operational knobs (pool sizes, batch sizes). Adding a key needs a code change, a migration that widens
   the CHECK, and review: an allow-list entry is a security decision, not an admin action.

3. **How `Settings` reads overrides safely.** `Settings` itself is untouched and stays immutable per process.
   `runtime_config.service.effective_settings(session, base)` reads the override rows and returns
   `base.model_copy(update=...)` (or `base` itself when there is none). A row whose key is not allow-listed or whose
   value is outside the bounds (possible only by editing the database by hand) is ignored and logged
   (`runtime_setting_ignored`), so the environment value applies: a bad row can never widen a limit past its bound.
   Every reader of an allow-listed field calls it at the point of the check, in the use case's own transaction:
   `payments/service.py` (per-transfer limit for transfers and scheduled-transfer creation, daily limit, scheduled
   transfer cap) and `bills/service.py` (per-payment and daily limits). `core/` does not import the module
   (import-linter contract unchanged). A unit test fails if any module outside `core/config.py` and
   `runtime_config/` reads an allow-listed field straight from `settings`.

4. **Cache and invalidation: none.** The overrides are read from PostgreSQL on every check (one indexed scan of a
   table of at most three rows, well under a millisecond, only on money-moving requests). The API processes and the
   Celery worker (scheduled transfers run through the same `PaymentsService`) therefore see a committed change on
   their next check, with no cache to invalidate, no pub/sub, and no window in which two processes enforce different
   limits. If measurements ever show this read matters, a per-process TTL cache can be added in a later ADR; it would
   trade a bounded staleness window for one query.

5. **Writes.** `GET /api/v1/admin/settings` and `PUT /api/v1/admin/settings/{key}` (body `{value, reason}`,
   `extra="forbid"`, reason 10-500 storable characters) are behind `require_roles(ADMIN, mfa=True)`. The change takes
   the audit-chain lock first (`AuditService.lock_chain`, as ADR-0027 does), reads the current effective value under
   it, rejects an out-of-bounds or unchanged value with 422 (no echo), upserts the row and appends
   `RUNTIME_SETTING_CHANGED` (`resource_type=runtime_setting`, `resource_id=key`, details
   `{key, old_value, new_value, default_value, reason}`) in the same transaction. Concurrent changes are therefore
   serialised and every audit row's `old_value` is exact. There is no separate "reset" endpoint: setting the value
   shown as the environment default restores the default behaviour (the row stays, and the screen says so).

## Alternatives considered
* **Redis-held overrides.** Shared by every process, but Redis is a cache in PBIP (fail-open rate limits, velocity
  counters); losing it would silently revert limits, and a change could not be committed atomically with its audit row.
* **Per-process cache with pub/sub or TTL invalidation.** Needs a new channel or accepts a staleness window during which
  the API and the worker disagree on a money limit; not justified by a sub-millisecond read.
* **Generic key/value table with JSON values and arbitrary keys.** More flexible, but turns the allow-list into data an
  admin could widen, and loses the typed DB CHECK. Rejected: the allow-list must be reviewed code.
* **A `settings_history` table.** Duplicates the audit log, which is already append-only, hash-chained and verified.
* **Mutating the cached `Settings` object.** Process-local, lost on restart, and not thread-safe.

## Consequences
* An ADMIN can tighten or relax the three limits without a deploy; the change applies to the next transfer, bill payment
  or scheduled-transfer creation in every process, and the scheduled-transfer worker on its next run.
* Money-moving requests make one extra small query per limit check.
* The environment value remains the documented default and the fallback; operators reading `.env` must also check the
  admin screen (the effective value and whether it is overridden are shown there).
* New attack surface (T50 in the threat model): a stolen ADMIN session with MFA can raise limits within the bounds. The
  bounds cap the damage, and every change is in the audit log with actor, request id, old and new value and reason.
* Removing an allow-list key later needs a migration that deletes its rows before narrowing the CHECK.
