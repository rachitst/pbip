# PBIP operations runbook

**Audience:** whoever is on call for a PBIP staging/production stack (platform owner, security owner, fraud analyst
lead). **Scope:** the stack defined by `infra/deploy/docker-compose.prod.yml` as deployed by
`.github/workflows/deploy.yml`. Everything here was checked against the repository at the time of writing; where a
step depends on infrastructure that the repository does not configure (TLS termination, WAL archive storage, host disk
encryption, off-host backup storage) it is marked **External** and must be set up and verified on the target host
before it can be relied on.

Conventions: `<placeholder>` values are yours to fill in; never paste secrets, tokens or customer data into tickets,
chat or this document; every command is run on the deployment host from `~/pbip` unless stated otherwise.

Related: [`docs/security/threat-model.md`](../security/threat-model.md) (controls and residual risks),
[`docs/SETUP_GUIDE.md`](../SETUP_GUIDE.md) (local setup), [`README.md`](../../README.md) (deploy trigger),
[`CONTRIBUTING.md`](../../CONTRIBUTING.md). Backup/restore automation is TASK-083 (#106); the staging VM pipeline is
TASK-082 (#105); this runbook documents the manual procedures those tasks will automate.

---

## 1. System map

### 1.1 Services (`infra/deploy/docker-compose.prod.yml`, project name `pbip-prod`)

| Service | Image | Role | Restart | Data |
|---|---|---|---|---|
| `web` | `${IMAGE_PREFIX}/web:${IMAGE_TAG}` (nginx, unprivileged) | Static React build; proxies `/api/` to `api`; the only published port (`${PUBLIC_PORT:-80}` → 8080) | unless-stopped | none |
| `api` | `${IMAGE_PREFIX}/api:${IMAGE_TAG}` | FastAPI (uvicorn) | unless-stopped | none |
| `migrate` | same as `api` | One-shot `alembic upgrade head`; `api` waits for it to complete | no | none |
| `worker` | same as `api` | Celery worker (outbox relay) | unless-stopped | none |
| `beat` | same as `api` | Celery beat: `pbip.outbox.relay` every 10 s | unless-stopped | schedule file in `/tmp` |
| `db` | `postgres:16-alpine` | PostgreSQL 16; not published | unless-stopped | volume `pgdata` |
| `redis` | `redis:7-alpine` (`--appendonly yes`) | Rate-limit counters, Celery broker/result backend | unless-stopped | volume `redisdata` |

Images are built and pushed to GHCR by the `package` job of `deploy.yml` when a `v*` tag is pushed; the `deploy` job
runs only when the repository variable `DEPLOY_ENABLED` is `true` and uses the secrets `DEPLOY_HOST`, `DEPLOY_USER`,
`DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and the variable `STAGING_URL` (see `README.md`, "Deploying").

### 1.2 Environment variables on the host (`~/pbip/.env`, template `infra/deploy/.env.prod.example`)

| Variable | Used by | Notes |
|---|---|---|
| `APP_ENV` | api, worker, beat | `staging` or `production`. In these values the API **refuses to boot** with the dev JWT secret, a JWT secret shorter than 32 characters, an empty `FIELD_ENCRYPTION_KEY`, `COOKIE_SECURE=false` or a CORS wildcard (`core/config.py`, tested by `test_production_rejects_unsafe_configuration`). |
| `JWT_SECRET` | api, worker, beat | HS256 signing key for access and MFA-challenge tokens. Rotation: §3.1 |
| `FIELD_ENCRYPTION_KEY` | api, worker, beat | Comma-separated Fernet keys; the **first** is used to encrypt, all of them to decrypt. Rotation: §3.2 |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | db, api (via `DATABASE_URL`) | Database credentials |
| `CORS_ORIGINS` | api | Comma-separated browser origins; no `*` |
| `COOKIE_SECURE` | api | Must be `true`; also enables HSTS on API responses |
| `LOG_LEVEL` | api, worker, beat | `INFO` unless debugging |
| `PUBLIC_PORT` | web | Host port published by `web` (default 80) |
| `IMAGE_PREFIX`, `IMAGE_TAG` | all app images | Set by the deploy job per release (`ghcr.io/<owner>/pbip`, `vX.Y.Z`); set them yourself for a manual deploy or rollback |
| `DAILY_TRANSFER_LIMIT_MINOR` | api | Optional override of the per-customer daily transfer limit (default `50000000` = ₹5,00,000.00). Emergency lever in §5.1 |
| `JWT_ACCESS_TTL_MINUTES` | api | Optional; default 15. Bounds how long a stolen access token stays valid (§5.2) |

### 1.3 Health, smoke test, logs, metrics

- Liveness `GET /api/health/live`; readiness `GET /api/health/ready` (checks PostgreSQL and Redis; result cached ~1 s).
- Smoke test after any change: `python scripts/smoke_test.py https://<host>` (or `make smoke https://<host>`; the
  deploy job runs `scripts/smoke_test.sh`, a wrapper around the same script). It checks readiness and the API security
  headers.
- Logs: every container writes JSON to stdout; `docker compose -f docker-compose.prod.yml logs --tail=200 api`.
  Where the monitoring stack is deployed (`make up-all` locally; not part of the prod compose file), Alloy ships all
  container stdout to Loki and Grafana's Explore can query by container. Logs are redacted (`core/logging.py`): never
  expect to find tokens, passwords, emails or account numbers in them, and never add them.
- Metrics: `/metrics` on the API container (not proxied by nginx: `infra/nginx/default.conf` returns 404). Prometheus
  rules in `infra/prometheus/alerts.yml`: `ApiHighErrorRate`, `ApiSlowP95`, `FraudScoringSlow`, `LoginFailureSpike`,
  `CeleryBacklog`. Grafana dashboard `infra/grafana/dashboards/pbip-overview.json` ("PBIP - Overview").
- Audit trail: `GET /api/v1/compliance/audit-logs` and `GET /api/v1/compliance/audit-logs/verify` (compliance officer
  or admin, MFA session) — the verify endpoint recomputes the hash chain.

### 1.4 TLS termination (**External**)

`web` listens on plain HTTP (nginx on 8080, published on `PUBLIC_PORT`). Nothing in the repository terminates TLS.
Put a TLS-terminating reverse proxy or load balancer in front of `PUBLIC_PORT` (certificate from your CA or ACME),
forward `X-Forwarded-Proto: https`, and keep `COOKIE_SECURE=true` so the refresh cookie is `Secure` and the API sends
HSTS. Verify from outside: `curl -sI https://<host>/api/health/live | grep -i strict-transport-security` must print a
header, and `curl -sI http://<host>/` should redirect to HTTPS at the proxy. Record the result on the deployment issue;
the threat model lists TLS as a deployment requirement, not an implemented control.

---

## 2. Routine operations

### 2.1 Deploy a release

1. From `main` with CI green: `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow accepts only
   `vX.Y.Z` tags and its `verify` job refuses a tag whose commit is not on `main`.
2. The `package` job builds and pushes `api:vX.Y.Z`/`web:vX.Y.Z` (and `latest`) to GHCR.
3. With `DEPLOY_ENABLED=true`, the `deploy` job (GitHub environment `staging`, one deployment at a time) checks
   that every required secret and variable is set (names only), copies `docker-compose.prod.yml` and
   `infra/deploy/deploy.sh` to `~/pbip`, and runs `bash deploy.sh deploy <prefix> vX.Y.Z` on the host:
   `pull`, then `up -d --remove-orphans --wait`, which runs `migrate` (`alembic upgrade head`) before `api` starts,
   starts `web` only once `api` is healthy, and fails if any container is not healthy within 180 s. The tag is
   recorded in `~/pbip/.deployed_tag` only after the stack is healthy; the previous release stays in
   `~/pbip/.previous_tag`.
4. The runner then runs `scripts/smoke_test.sh` against `STAGING_URL`. If the deploy or the smoke test fails, the
   workflow runs `bash deploy.sh rollback` on the host, which redeploys the previous healthy release (§2.2), and the
   run is marked failed.
5. Confirm: the workflow is green; `docker compose -f docker-compose.prod.yml ps` on the host shows `migrate`
   exited 0 and the others healthy; `cat ~/pbip/.deployed_tag` shows the new tag.

**Cutover window.** Containers are recreated in place on one host, so the API is unavailable for the seconds
between the old `api` stopping and the new one passing its health check (nginx is restarted after that). This is
a health-gated, minimal-downtime deployment, not a zero-downtime one; true zero downtime needs two `api` replicas
behind a dynamically resolved nginx upstream (follow-up noted on #105).

**Dry run.** `Actions -> Deploy -> Run workflow` with a tag and `dry_run = true` runs the tag verification, the image
build and the secret/variable check, prints the plan, and skips every step that touches the host.

### 2.2 Roll back

- **From GitHub:** `Actions -> Deploy -> Run workflow` with `tag = v<previous>` redeploys that release through the
  same health-gated path (images are immutable per tag).
- **On the host:** `cd ~/pbip && bash deploy.sh rollback` restarts the release recorded in `.previous_tag`.
- **Automatic:** the workflow does this itself when a deploy or smoke test fails.

Migrations are **not** rolled back automatically. Every migration has a tested `downgrade` (CI runs up → down → up),
but only downgrade when the previous release cannot run on the new schema, and take a backup first (§4.1):
`docker compose -f docker-compose.prod.yml run --rm migrate alembic -c /app/apps/api/alembic.ini downgrade -1`.

### 2.3 Restart one service / apply a changed `.env`

`docker compose -f docker-compose.prod.yml up -d api worker beat` recreates the containers whose environment changed
(compose compares the resolved config). `restart` alone does **not** reload `.env`.

---

### 2.4 Refresh the GeoIP country database (monthly, optional)

The NEW_COUNTRY fraud rule (ADR-0014) needs DB-IP's free country database. Without it the rule is
off and nothing else changes. DB-IP publishes a new file each month (CC BY 4.0).

1. On a machine with the repository: `uv run python scripts/fetch_geoip.py --month YYYY-MM --dest geoip/dbip-country-lite.mmdb`.
   The script downloads, decompresses, verifies two known addresses and replaces the file atomically;
   it prints the SHA-256 (record it in the change log).
2. Copy the file to the host as `~/pbip/geoip/dbip-country-lite.mmdb` (mounted read-only into `api`
   at `/data/geoip`), and set `GEOIP_COUNTRY_DB_PATH=/data/geoip/dbip-country-lite.mmdb` in `~/pbip/.env`.
3. Apply it as in 2.3 (restart `api`). The reader is opened once per process.

Keep the attribution "IP Geolocation by DB-IP" (https://db-ip.com) wherever location results are
shown (the staff alert view shows the NEW_COUNTRY reason text).

## 3. Secret rotation

Both procedures are safe to run during business hours; §3.1 logs every active user out of their *access* token for at
most one request (the browser refreshes silently), §3.2 is invisible to users.

### 3.1 Rotate `JWT_SECRET`

**Effect.** Access JWTs (15 min) and MFA-challenge tokens are signed with HS256 using this value
(`core/security.py`). Changing it invalidates every outstanding access token immediately. Refresh tokens are opaque
random values stored hashed in PostgreSQL (`refresh_tokens`) and are **not** affected: the browser's next
`POST /api/v1/auth/refresh` returns a new access token signed with the new secret, so customers and staff stay signed
in. A user in the middle of the MFA step will have to log in again. Because there is a single key (no `kid`
rotation yet; threat model §5.1), there is no overlap period: every API replica must pick up the new value in the same
rollout.

**When.** On a fixed schedule you choose (e.g. quarterly), after any suspected exposure of the host `.env`, a CI
secret, or a backup that contained it, and as the kill switch in §5.2 step 4.

**Steps.**

1. Generate a new value (at least 32 characters; the API enforces this in deployed environments):
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
2. Keep the current value somewhere you can restore from for 24 h (your secret store, not a file on the host).
3. Edit `~/pbip/.env`: `JWT_SECRET=<new value>`.
4. Apply: `docker compose -f docker-compose.prod.yml up -d api worker beat`.
5. Verify:
   - `python3 scripts/smoke_test.py https://<host>` passes.
   - An access token issued before the rotation is rejected: `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <old token>" https://<host>/api/v1/auth/me` prints `401`.
   - A browser session that was signed in before the rotation keeps working after one request (it refreshes).
   - `security_events` shows a burst of `TOKEN_REFRESHED` and no unusual `LOGIN_FAILURE` growth
     (`docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT event_type, count(*) FROM security_events WHERE created_at > now() - interval '15 minutes' GROUP BY 1;"`).
6. Record the rotation (date, who, reason) on the operations issue. Do not record the value.

**Rollback.** Put the previous value back and repeat step 4. Tokens issued under the new value become invalid; the
refresh flow recovers sessions exactly as above.

### 3.2 Rotate `FIELD_ENCRYPTION_KEY`

**Effect.** `FieldCipher` (`core/crypto.py`) wraps `MultiFernet`: the first key encrypts, every listed key decrypts.
Encrypted columns today: `users.mfa_secret_enc` (staff TOTP secrets) and `customers.phone_enc`. Rotation is a
two-phase change so that nothing is unreadable at any point: add the new key in front of the old one, re-encrypt
every row with `scripts/rotate_field_key.py`, then drop the old key.

**Steps.**

1. Generate a key: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Edit `~/pbip/.env` so the new key comes **first** and the old key stays: `FIELD_ENCRYPTION_KEY=<new>,<old>`.
3. Apply: `docker compose -f docker-compose.prod.yml up -d api worker beat`. New writes now use the new key; existing
   rows still decrypt with the old key. Verify a staff login with TOTP still works (it decrypts the secret).
4. Dry run: `docker compose -f docker-compose.prod.yml exec api python scripts/rotate_field_key.py`. It prints how
   many rows per column it would re-encrypt, and **fails without writing** if any row cannot be decrypted with the
   configured keys (which means step 2 lost the old key; fix `.env` and retry).
5. Apply: `docker compose -f docker-compose.prod.yml exec api python scripts/rotate_field_key.py --apply`. All rows
   are rewritten in one transaction. Re-running is harmless.
6. Edit `~/pbip/.env` to the new key only: `FIELD_ENCRYPTION_KEY=<new>`, then apply as in step 3.
7. Verify: a staff login with TOTP succeeds; `scripts/rotate_field_key.py` (dry run) reports the same counts and no
   error, which proves every row decrypts with the new key alone.
8. Destroy the old key in your secret store after the next verified backup (§4.1), because backups taken before step 5
   contain rows encrypted with the old key. Keep the old key in the secret store for as long as such a backup must stay
   restorable, and note the key generation in the backup's record.

**Rollback.** Before step 6, simply restore `FIELD_ENCRYPTION_KEY=<old>,<new>` order or `<old>` alone only if step 5
was not run; after step 5, rows are under the new key, so the new key must remain in the list.

---

## 4. Database backup and point-in-time recovery

PostgreSQL 16 runs in the `db` container with its data in the `pgdata` volume. The repository ships **no** backup
automation (that is TASK-083) and **no** WAL archiving configuration; the procedures below are manual and the
archive/off-host storage is **External**. Redis holds only rate-limit counters and the Celery queue, which are safe
to lose; its AOF file in `redisdata` restores the queue after a restart.

### 4.1 Logical backup (daily, and before every migration or secret rotation)

```bash
cd ~/pbip
set -a; . ./.env; set +a
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  | gpg --symmetric --cipher-algo AES256 --batch --passphrase-file /root/.pbip-backup-passphrase \
  > /var/backups/pbip/pbip-$STAMP.dump.gpg
sha256sum /var/backups/pbip/pbip-$STAMP.dump.gpg > /var/backups/pbip/pbip-$STAMP.dump.gpg.sha256
```

- `-Fc` (custom format) allows selective and parallel restore. The dump contains encrypted MFA secrets and the hashed
  credentials, so the file itself is encrypted; the passphrase file must be `chmod 600`, owned by root and backed up in
  the secret store, never alongside the dumps.
- Copy the `.gpg` and `.sha256` files off the host (object storage with versioning and a retention policy you choose;
  **External**). A backup that exists only on the host it protects is not a backup.
- Record in the backup log: timestamp, size, SHA-256, the `FIELD_ENCRYPTION_KEY` generation in use (§3.2 step 8) and
  the release tag.

### 4.2 Restore drill (monthly) and integrity check

Restore into a **temporary database on the same server** so the live stack is untouched:

```bash
cd ~/pbip
set -a; . ./.env; set +a
F=/var/backups/pbip/pbip-<stamp>.dump.gpg
sha256sum -c "$F.sha256"
docker compose -f docker-compose.prod.yml exec -T db psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS pbip_restore_test;" -c "CREATE DATABASE pbip_restore_test;"
gpg --decrypt --batch --passphrase-file /root/.pbip-backup-passphrase "$F" \
  | docker compose -f docker-compose.prod.yml exec -T db pg_restore -U "$POSTGRES_USER" -d pbip_restore_test --no-owner --exit-on-error
```

Verify the restored copy:

1. Row counts match the source for the core tables:
   `docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_USER" -d pbip_restore_test -c "SELECT 'users', count(*) FROM users UNION ALL SELECT 'accounts', count(*) FROM accounts UNION ALL SELECT 'transactions', count(*) FROM transactions UNION ALL SELECT 'transaction_entries', count(*) FROM transaction_entries UNION ALL SELECT 'audit_logs', count(*) FROM audit_logs;"`
   and the same query against `$POSTGRES_DB` taken at backup time.
2. The ledger balances: `SELECT count(*) FROM accounts a WHERE a.ledger_balance <> (SELECT coalesce(sum(CASE WHEN direction='CREDIT' THEN amount ELSE -amount END),0) FROM transaction_entries e WHERE e.account_id=a.id);` must return `0`.
3. **Hash-chain continuity** of the audit log in the restored copy, using the same code the API's verify endpoint
   runs (`AuditService.verify_chain`, `compliance/service.py`):

   ```bash
   docker compose -f docker-compose.prod.yml exec \
     -e DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/pbip_restore_test" \
     api python -c "from pbip_api.core.db import get_engine; from sqlalchemy.orm import Session; from pbip_api.modules.compliance.service import AuditService; s = AuditService(Session(get_engine())).verify_chain(); print(s); raise SystemExit(0 if s.valid else 1)"
   ```

   `valid=True` with `checked` equal to the `audit_logs` row count means no row was lost, reordered or altered between
   backup and restore. `valid=False` names the first bad `seq`: treat the backup as compromised and investigate.
4. Clean up: `... psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE pbip_restore_test;"`.
5. Record the drill (backup used, timings, counts, chain result) on the operations issue. A restore that was never
   drilled does not count as a recovery capability.

### 4.3 Full restore of the live database

Only after the decision in §5 (or a confirmed data loss), with the team informed:

1. `docker compose -f docker-compose.prod.yml stop web api worker beat` (keep `db` and `redis` up).
2. Take a safety dump of the current state as in §4.1 (even if it is believed corrupt).
3. Recreate: `... psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$POSTGRES_DB\";" -c "CREATE DATABASE \"$POSTGRES_DB\";"` then `pg_restore` into it as in §4.2 (`-d "$POSTGRES_DB"`).
4. `docker compose -f docker-compose.prod.yml up -d` (runs `migrate`, which is a no-op if the backup is at head, or
   applies migrations released after the backup).
5. Run the §4.2 checks 1–3 against the live database and the smoke test, then
   `GET /api/v1/compliance/audit-logs/verify` as a compliance officer.
6. Transactions that posted after the backup are lost; the outbox events for them are lost too. Reconcile from the
   previous day's statements/exports if any, and tell affected customers.

### 4.4 Point-in-time recovery (PITR) — **External** configuration required

Logical dumps lose everything after the dump. PITR needs continuous WAL archiving plus periodic base backups; the
repository's compose file does not enable it. To enable it on the host:

1. Add an archive volume and the archiving settings to the `db` service in a compose override
   (`docker-compose.prod.override.yml`, kept on the host, not in the repository):

   ```yaml
   services:
     db:
       command: ["postgres", "-c", "wal_level=replica", "-c", "archive_mode=on",
                 "-c", "archive_command=test ! -f /wal_archive/%f && cp %p /wal_archive/%f",
                 "-c", "archive_timeout=300"]
       volumes:
         - pgdata:/var/lib/postgresql/data
         - /var/backups/pbip/wal_archive:/wal_archive
   ```

   Apply with `docker compose -f docker-compose.prod.yml -f docker-compose.prod.override.yml up -d db`. Ship
   `/var/backups/pbip/wal_archive` off the host continuously (**External**).
2. Base backup (daily):
   `docker compose -f docker-compose.prod.yml exec -T db pg_basebackup -U "$POSTGRES_USER" -D - -Ft -z -X stream > /var/backups/pbip/base-$(date -u +%Y%m%dT%H%M%SZ).tar.gz`
   then encrypt and ship it like a logical dump (§4.1).
3. Recovery to a point in time (into a *new* data directory; never over the live one):
   - Stop the app services; create an empty directory, extract the base backup into it.
   - Create `recovery.signal` in it and set in its `postgresql.auto.conf`:
     `restore_command = 'cp /wal_archive/%f %p'` and `recovery_target_time = '<UTC timestamp>'` (plus
     `recovery_target_action = 'promote'`).
   - Start a PostgreSQL 16 container on that directory with the WAL archive mounted; it replays WAL up to the target,
     then promotes. Run the §4.2 checks (counts, balances, hash chain) before pointing the stack at it.
4. Until this is configured and a recovery has been drilled on the real host, state the recovery point objective
   honestly as "last logical dump" (§4.1), not "point in time".

---

## 5. Incident response playbooks

Shared rules: preserve evidence before changing anything (export rows, keep logs); act as the smallest role that can do
the step (fraud analyst for alerts, admin with MFA for user actions); every containment action below is recorded in
`security_events`/`audit_logs` or in the operations issue; never message customers from personal channels; no
reputational statements until the security owner signs off.

### 5.1 Fraud spike

**Signal.** Grafana "Fraud decisions" panel or `rate(fraud_decisions_total{decision=~"REVIEW|BLOCK"}[5m])` rising well
above the daily norm; the alert queue (`GET /api/v1/fraud/alerts`) growing faster than analysts clear it;
`LoginFailureSpike` or `TOKEN_REUSE_DETECTED` events preceding it (account-takeover pattern); customer reports.

**Triage (fraud analyst, MFA session), first 15 minutes.**
1. Open the queue newest-first; read `reasons` and the feature snapshot on each alert (`GET /api/v1/fraud/alerts/{id}`):
   which rule codes dominate (`NEW_PAYEE_LARGE_AMOUNT`, `VELOCITY_BURST`, `AMOUNT_SPIKE`, `LARGE_AMOUNT`,
   `NEW_PAYEE`; `fraud/baseline.py`)?
2. Classify: (a) a few customers with many alerts → compromised accounts or mules; (b) many customers, one rule → a
   rule misfiring or a legitimate event (payday, a bill-pay batch); (c) one destination account/payee receiving from
   many → mule collection account.
3. Decide alerts individually with `POST /api/v1/fraud/alerts/{id}/decision` (`RELEASE` or `REJECT`). Held
   transfers stay held while in review; a `REJECT` declines and releases the hold.

**Contain (platform owner).**
4. For pattern (a) or (c): disable the involved customer accounts' users and revoke their sessions (SQL in §5.2 step 2,
   using the customer's `user_id`). Their held transfers remain for analyst decision.
5. For a broad attack where losses are running: lower the daily transfer limit for everyone until it stops, e.g.
   `DAILY_TRANSFER_LIMIT_MINOR=1000000` (₹10,000) in `~/pbip/.env`, then
   `docker compose -f docker-compose.prod.yml up -d api`. This is immediate and reversible, and it is enforced inside
   the payments service, so it also applies to transfers already in `PENDING_STEP_UP`. Tell support what customers
   will see (a limit error on larger transfers).
6. Rule thresholds and weights are code constants in `fraud/baseline.py`, not configuration. Tightening them is a
   hotfix PR through the normal pipeline (tests required; the behaviour-regression expectations in `test_transfers.py`
   must be updated deliberately, never deleted) followed by a tagged release (§2.1). Do not edit code on the host.
7. If the spike coincides with a scorer failure (`FraudScoringSlow`, errors in `api` logs), remember the design
   rule: the scorer must fail to rules-only, never to allow-all. If transfers are posting with no `fraud_events` rows,
   stop money movement by setting `DAILY_TRANSFER_LIMIT_MINOR=1` until fixed.

**Recover and close.**
8. Restore the limit, re-enable wrongly disabled users (`UPDATE users SET status='ACTIVE' ...`; they must log in again
   and re-enrol MFA if you also cleared it), and release false positives.
9. Export evidence: `fraud_events`, `fraud_alerts`, the affected `transactions`, `security_events` and `audit_logs`
   for the window (`psql ... \copy (SELECT ...) TO '/tmp/...csv'`), store with the incident record (encrypted,
   access-controlled; it contains customer identifiers).
10. Post-incident: add the pattern to the threat model §7 and to the synthetic scenario list; file a backlog task for
    any rule or feature gap found; review analyst decision latency.

### 5.2 Compromised staff session

A fraud analyst, compliance officer or admin credential, TOTP secret, device or active session is suspected stolen
(reports of actions the person did not take, `TOKEN_REUSE_DETECTED` on their user, logins from an unknown IP, admin
unlocks they do not recognise, audit entries they deny).

**Facts that shape the response** (from the code; threat model §5): access JWTs are validated by signature only — the
API does not consult the database on each request (`auth/dependencies.py`) — so disabling a user stops their
*refresh* and *login* immediately but a stolen access token keeps working until it expires (default 15 min,
`JWT_ACCESS_TTL_MINUTES`). Refresh tokens live in `refresh_tokens` and can be revoked server-side. Staff routes also
require `amr: otp`, so a stolen password alone is not enough; a stolen *session* is.

**Steps.**

1. **Preserve evidence first** (two minutes):
   `docker compose -f docker-compose.prod.yml exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\copy (SELECT * FROM security_events WHERE user_id='<user_id>' ORDER BY created_at) TO '/tmp/se-<user_id>.csv' CSV HEADER" -c "\copy (SELECT * FROM audit_logs WHERE actor_user_id='<user_id>' ORDER BY seq) TO '/tmp/al-<user_id>.csv' CSV HEADER" -c "\copy (SELECT id, family_id, issued_at, used_at, revoked_at, ip, user_agent FROM refresh_tokens WHERE user_id='<user_id>') TO '/tmp/rt-<user_id>.csv' CSV HEADER"`
   then copy the files off the container (`docker compose ... cp db:/tmp/se-<user_id>.csv ./evidence/`). Find the
   `user_id` by email: `SELECT id, email, status FROM users WHERE lower(email)=lower('<email>');`.
2. **Contain the account** (one transaction):
   ```sql
   BEGIN;
   UPDATE users SET status = 'DISABLED' WHERE id = '<user_id>';
   UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = '<user_id>' AND revoked_at IS NULL;
   COMMIT;
   ```
   Login and refresh now fail for this user (`UserStatus.DISABLED` is checked on both paths). Confirm:
   `SELECT status FROM users WHERE id='<user_id>'; SELECT count(*) FROM refresh_tokens WHERE user_id='<user_id>' AND revoked_at IS NULL;` → `DISABLED`, `0`.
3. **Bound the remaining exposure.** The attacker's current access token works for up to `JWT_ACCESS_TTL_MINUTES`
   more. During that window, watch the audit log for the actor: `SELECT seq, created_at, action, resource_type, resource_id FROM audit_logs WHERE actor_user_id='<user_id>' AND created_at > now() - interval '30 minutes' ORDER BY seq;`
4. **Kill switch when the window is unacceptable** (admin account, money-movement decisions in progress, or any
   doubt): rotate `JWT_SECRET` now (§3.1). This invalidates *every* access token platform-wide within one rollout;
   other users recover transparently through refresh, the disabled user cannot.
5. **Review what was done with the session.** Walk the exported `audit_logs`: fraud decisions (`RELEASE` of held
   transfers), admin unlocks, user creations. Reverse what can be reversed through the normal paths (a released
   transfer that posted needs the ledger's compensating reversal through the payments owner; a created admin user is
   disabled with the SQL in step 2). Verify the chain afterwards: `GET /api/v1/compliance/audit-logs/verify` must be
   valid; if it is not, the database itself was tampered with and §4 (restore from the last good backup and PITR, if
   configured) becomes the incident.
6. **Re-credential the person.** There is no admin endpoint to reset another user's password or MFA. Create a
   replacement staff user with `POST /api/v1/admin/users` (admin with MFA), have the person enrol MFA on a clean
   device, and keep the compromised user `DISABLED` permanently (audit rows reference it by id and must stay
   resolvable).
7. **Widen the search.** Same IP/user-agent in `security_events` across other users? Other `TOKEN_REUSE_DETECTED` in
   the last 7 days? `SELECT user_id, ip, count(*) FROM security_events WHERE event_type='TOKEN_REUSE_DETECTED' AND created_at > now() - interval '7 days' GROUP BY 1,2;`
8. **Close.** Incident record with timeline, evidence file hashes, actions reversed, and follow-ups: an access-token
   denylist and admin "revoke sessions"/"disable user" endpoints are open items in the threat model (§10) and this
   playbook is the reason to prioritise them.

---

## 6. Verification checklist for this runbook

Run these after any change to the runbook or the stack and record the date on the operations issue:

- [ ] §2.1 deploy of a tag and §2.2 rollback to the previous tag, each followed by a passing smoke test.
- [ ] §3.1 rotation on staging: old access token → 401, a signed-in browser survives, smoke test passes.
- [ ] §3.2 rotation on staging: dry run, apply, old key removed, staff TOTP login works, dry run reports no error.
- [ ] §4.1 backup produced, encrypted, hashed and copied off-host; §4.2 drill restores it and the hash chain verifies.
- [ ] §4.4 only if WAL archiving is configured: a PITR to a chosen timestamp drilled on the real host.
- [ ] §5.2 steps 1–2 rehearsed on staging with a test staff user; `DISABLED` user cannot refresh or log in.
- [ ] `apps/api/tests/unit/test_runbook_references.py` passes (every file, `make` target, API path and variable this
      document cites still exists).

None of the staging/production checks above has been run yet as of the first version of this runbook (no
staging host exists; TASK-082 creates it). The repository-level checks (script tests, reference test) run in CI.
