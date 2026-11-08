# ADR 0042: Customer PII encryption policy for email, full name and date of birth
- Status: Accepted (under owner's standing approval, 2026-11-03). No longer provisional since 2026-11-08: the at-rest
  controls the decision rests on are demonstrated on the staging host (#399, "Update 2026-11-08" below). The phone
  condition is met (2026-11-04, #398): US-17 requires the profile to show a masked phone number, so the column was
  given a write path rather than removed (see "Update 2026-11-04").
- Date: 2026-11-03
- Deciders: Architecture Team (issue #145, from the TASK-028 threat-model review)

## Context
`users.email`, `customers.full_name` and `customers.date_of_birth` are plain-text columns. Field-level encryption
(Fernet through `MultiFernet`, `core/crypto.FieldCipher`, key from `FIELD_ENCRYPTION_KEY`) is used only for
`users.mfa_secret_enc` and `customers.phone_enc`. Issue #145 asks for a documented, risk-based choice on whether the
three plain columns also need field-level encryption, resting on the baseline that is actually in the repository.
UK GDPR Art. 25 and 32 (threat model §9) ask for appropriate measures, encryption *where appropriate*; neither
requires field encryption of a name or a date of birth. PBIP is a teaching simulation, not a regulated firm.

The issue's inventory dates from 2026-10-28 (`b4d07ec`). Re-verified on `main` `308a041`:

| Control | State in the repository | Evidence |
|---|---|---|
| Field-level encryption | `users.mfa_secret_enc` and `customers.phone_enc` only; both are re-encrypted by key rotation. `email` (unique `lower(email)` index, the login key), `full_name` (`String(200)`) and `date_of_birth` (`Date`) are plain | `core/crypto.py`, `auth/models.py`, `customers/models.py`, `scripts/rotate_field_key.py` (`ENCRYPTED_COLUMNS`); `test_crypto.py` (`test_key_rotation_new_key_first_old_key_still_decrypts`), `test_rotate_field_key.py`; **new** `test_pii_encryption_policy.py` pins this inventory |
| Phone write path | Dormant when this ADR was written: `phone_enc` was decrypted and masked in `CustomerService.get_profile`, but nothing wrote it. **Implemented 2026-11-04 (#398)**, see "Update 2026-11-04" | `customers/service.py`; `test_get_customer_me_with_masked_phone`, `test_customer_phone.py` |
| Key management | Staging/production refuse to boot with `COOKIE_SECURE=false`, an empty `FIELD_ENCRYPTION_KEY`, a CORS wildcard, or a dev or too-short JWT secret. Two-phase rotation runbook | `core/config.py`; `test_production_rejects_unsafe_configuration`; runbook §3.2 |
| TLS, browser to nginx | **Now in the repository** (was "not in the repo"). nginx serves TLS 1.2/1.3 with AEAD ciphers on 8443 (443 on a host), `:8080` only redirects (and serves ACME); HSTS `max-age=63072000; includeSubDomains` on static responses, and from the API when `COOKIE_SECURE=true`. Certificates: `scripts/dev_certs.py` locally, certbot on a host | `infra/nginx/default.conf.template`, `infra/nginx/security-headers.conf`; `scripts/verify_tls.sh` (CI e2e job); `test_production_enables_hsts_and_disables_docs`; runbook §1.4 |
| TLS to PostgreSQL | **Now in the repository.** `sslmode=verify-full` against the local/host CA for the API, worker and beat (and the local postgres-exporter); `pg_hba` accepts TCP only as `hostssl` and rejects plain `host` | `docker-compose.yml`, `infra/deploy/docker-compose.prod.yml`, `infra/postgres/tls-entrypoint.sh`; `scripts/verify_tls.sh` |
| TLS nginx → API, services → Redis | Not configured: plain TCP on the internal compose network, not published. Redis carries rate-limit counters, velocity counters and the Celery queue (beat-scheduled tasks, no customer PII in task arguments) | runbook §1.4 table; threat model §1 |
| Database volume at rest | **None in the repository.** `pgdata` is a plain Docker volume; disk encryption is the host's job (runbook: **External**) and has not been demonstrated on any host | `infra/deploy/docker-compose.prod.yml`; follow-up **#399** |
| Backups | **Now in the repository** (was "none"). `pg_dump -Fc` streamed into `gpg` (AES-256 symmetric with a chmod-600 passphrase file, or public-key), plaintext never on disk, files mode 600; restore drill verifies row counts, ledger totals and the audit chain. Drilled on the local stack only (2026-11-01); scheduling and off-host storage are **External** | `scripts/backup.py`, `scripts/restore.py`; `test_backup_restores_completely_and_the_drill_cleans_up` (asserts the file is not a raw dump and is mode 600), `test_symmetric_encryption_reads_the_passphrase_from_a_file`, `test_passphrase_file_must_be_private`; runbook §4 |
| Access control | Customer profile and date-of-birth routes only through `get_current_customer` (self, no id in the path); staff routes behind `require_roles(..., mfa=True)`; RM views show the name but no email, birth date or account number | `customers/router.py`, `insights/schemas.py`; `test_rbac_bola.py`, `test_route_policy_registry.py` (`test_every_insights_route_is_behind_the_mfa_rm_gate`), `test_staff_responses_never_contain_the_date_of_birth` |
| Logging | Redaction by key now includes `full_name`, `date_of_birth`, `phone`, `birth`, `dob` (#148, TASK-107), plus email/JWT/bearer/long-digit patterns | `core/logging.py`; `test_customer_pii_keys_are_redacted_at_any_depth`, `test_identifier_keys_are_not_over_redacted_by_the_pii_parts`, `test_end_to_end_log_line_never_contains_customer_pii`, `test_date_of_birth_never_reaches_the_logs` |
| Outbound data | The optional Groq router (ADR-0037) receives only the question after removal of emails, ids, account and phone numbers and the customer's own name | `assistant/providers.py`; `test_outgoing_question_has_no_identifiers_or_pii`, `test_own_name_is_removed_whole_words_only_and_short_parts_are_kept` |

## Decision
Do **not** add field-level encryption to `users.email`, `customers.full_name` or `customers.date_of_birth` now.
Protect them with the baseline above (TLS in transit, encrypted backups, MFA-gated staff access with object-level
authorisation, log redaction, data minimisation in staff and AI responses) plus host disk encryption on the
deployment target. Field-level encryption stays reserved for secrets and identifiers that are never searched,
sorted or shown in full (`mfa_secret_enc`, `phone_enc`). The decision was **provisional** until #399 recorded host
disk encryption and an off-host, encrypted, drilled backup on the deployment host; both are recorded since
2026-11-08 ("Update 2026-11-08" below).

`test_pii_encryption_policy.py` makes the inventory executable: it fails if a new `*_enc` column is not covered by
key rotation, if a plain-text phone column appears, or if the case-insensitive unique email index changes.

## Alternatives considered
- **Fernet on all three columns.** Fernet is randomised (fresh IV per token), so equal emails give different
  ciphertexts: the unique `lower(email)` index, login lookup, duplicate-registration check and staff search would
  all break. Names would lose ordering and search in staff views, and statements (TASK-016) and KYC-style display
  would decrypt on every read. Against the threats it addresses (a stolen volume or dump), disk and backup
  encryption already cover the data, while an attacker inside the API process holds the key anyway.
- **Deterministic encryption or a blind index for email** (HMAC of `lower(email)` for lookup, Fernet for the value).
  Keeps login and uniqueness, but adds a second key and a re-index on every rotation, and a keyed hash of a
  low-entropy value is still linkable. Too much new key management for a teaching system; revisit if a real
  deployment's risk assessment asks for it.
- **PostgreSQL `pgcrypto` column encryption.** Puts the key in SQL statements (and so in the server's logs and
  `pg_stat_statements`), which is worse than application-side encryption.
- **Encrypt only `date_of_birth`.** It is used for an age band and shown only to the customer, never looked up, so it
  could be encrypted. Not done now: it is optional, consented and never shown to staff, and the gain over disk
  encryption is small; it is the first candidate if #399 cannot be met.

## Consequences
- No schema, API or behaviour change. Customer-visible behaviour is unchanged.
- Residual risks, recorded in threat model §3 A02 and §9:
  - Anyone with database read access (a DBA role, a SQL injection, a leaked `DATABASE_URL`) reads email, name and
    date of birth in plain text. Mitigations are access control, parameterised queries (SQLAlchemy) and the
    `verify-full` TLS requirement, not encryption.
  - ~~The `pgdata`, `redisdata` and log volumes are unencrypted unless the host encrypts its disk.~~ Resolved
    2026-11-08 on the staging host (below); any other host must show the same before it holds customer data.
  - nginx → API and services → Redis traffic is plain TCP on the internal network (accepted for a single host).
  - ~~Backups are encrypted but only drilled locally.~~ Resolved 2026-11-08: scheduled, off-host and drilled on the
    staging host (below). Key custody (the passphrase in the owner's password manager) stays an operator task.
  - ~~The phone column is encrypted but never written (#398).~~ Resolved 2026-11-04: the number is written
    encrypted (below).
- Revisit when: #399 cannot show disk encryption (then encrypt `date_of_birth` first), a real phone write path or
  any new identifier (national id, address) is added (field-encrypt it by default and add it to
  `rotate_field_key.ENCRYPTED_COLUMNS`), or the system moves beyond a single host.
- Standing approval: (1) no security control is weakened, a guard test is added; (2) no money movement;
  (3) no migration; (4) no new service, dependency or outbound data; (5) no invented metrics, every row cites a
  file or test, and undemonstrated controls are marked as such; (6) within issue #145's scope.

## Update 2026-11-04: phone write path (#398)
Decided by the requirements: US-17 (FR-16, "manage account settings") has the acceptance criterion "The profile
shows a masked phone number". A number nobody can store cannot be shown, so removing the column would drop a
requirement. The column was therefore given a write path, following this ADR's rule for a new identifier
(field-encrypted by default, covered by key rotation):

- `PUT /api/v1/customers/me/phone` (strict E.164) and `DELETE /api/v1/customers/me/phone`, only for the
  authenticated customer (`get_current_customer`; staff 403), per-customer rate limit
  `RATE_LIMIT_PHONE_CHANGE_PER_HOUR` (default 10).
- Stored only as `FieldCipher` ciphertext; the write refuses to run without `FIELD_ENCRYPTION_KEY`; responses
  show only the last four digits. Saving the same number is a no-op (decrypt and compare, since Fernet is
  randomised).
- Audit `PHONE_SET` (`replaced_existing` only) and `PHONE_DELETED`, never the number.
- The number is contact data only: it is not used for sign-in, MFA or account recovery, and no SMS is sent.
- Profile & security has a "Mobile number" section.
- Threat model row T66; tests in `test_customer_phone.py` and the BOLA matrix.

Standing approval for this update: (1) no control weakened; the new route is customer-only, validated, audited
and rate-limited; (2) no money movement; (3) no migration (the column already exists); (4) no new service,
dependency or outbound data; (5) no metrics; (6) within US-17 / FR-16.

## Update 2026-11-08: at-rest evidence on the staging host (#399)
The conditions that kept this decision provisional are met on the staging host (Oracle Cloud Always Free A1, Ubuntu
24.04); the evidence is on #399, commands and outcomes only:

| Control | Evidence |
|---|---|
| Disk encryption | OCI console, boot volume encryption: **Oracle-managed key** (AES-256, the OCI default). Docker's data root `/var/lib/docker`, and with it `pgdata`, `redisdata` and the logs, is on that boot volume (`lsblk`: one disk; no block volume) |
| Off-host encrypted backups | `pbip-backup.timer` daily 02:30 UTC: `backup.py` (gpg) and the manifest copied to OCI Object Storage (`infra/deploy/backup-offsite.sh`, `scripts/offsite_backup.py`, deploy guide section 15); first scheduled run 2026-11-08T02:30Z uploaded both. The passphrase is its own file, separate from `.env` and `FIELD_ENCRYPTION_KEY` |
| Restore drill on the host | `restore.py` PASS on the copy downloaded back from the bucket (revision 0031, ledger net 0, audit chain valid) |
| TLS in transit from outside | Let's Encrypt certificate; `http://` answers 301 to `https://`; HSTS `max-age=63072000; includeSubDomains`; TLSv1.3; the Deploy workflow's smoke test passes over HTTPS |

What this does not cover: an Oracle-managed key protects against exposure of the provider's media, not against
someone with root on the running instance or the database credentials; those threats are handled by access control
and are why the residual risks above stay. A customer-managed key (OCI Vault) is not used; the OCI default meets #399's
criterion, and switching keys is a console change that needs no change here.
