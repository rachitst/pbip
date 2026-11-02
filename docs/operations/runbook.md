# PBIP operations runbook

**Audience:** whoever is on call for a PBIP staging/production stack (platform owner, security owner, fraud analyst
lead). **Scope:** the stack defined by `infra/deploy/docker-compose.prod.yml` as deployed by
`.github/workflows/deploy.yml`. Everything here was checked against the repository at the time of writing; where a
step depends on infrastructure that the repository does not configure (DNS and the certbot install, WAL archive storage, host disk
encryption, off-host backup storage) it is marked **External** and must be set up and verified on the target host
before it can be relied on.

Conventions: `<placeholder>` values are yours to fill in; never paste secrets, tokens or customer data into tickets,
chat or this document; every command is run on the deployment host from `~/pbip` unless stated otherwise.

Related: [`docs/security/threat-model.md`](../security/threat-model.md) (controls and residual risks),
[`docs/SETUP_GUIDE.md`](../SETUP_GUIDE.md) (local setup), [`README.md`](../../README.md) (deploy trigger),
[`CONTRIBUTING.md`](../../CONTRIBUTING.md). Backup and restore drills are scripted (TASK-083, §4); the staging VM
pipeline is TASK-082 (#105).

---

## 1. System map

### 1.1 Services (`infra/deploy/docker-compose.prod.yml`, project name `pbip-prod`)

| Service | Image | Role | Restart | Data |
|---|---|---|---|---|
| `web` | `${IMAGE_PREFIX}/web:${IMAGE_TAG}` (nginx, unprivileged) | Terminates TLS (`${PUBLIC_HTTPS_PORT:-443}` → 8443); HTTP (`${PUBLIC_PORT:-80}` → 8080) only redirects and answers ACME challenges; static React build; proxies `/api/` to `api`. The only published ports | unless-stopped | `./certs/web`, `./acme` (read-only) |
| `api` | `${IMAGE_PREFIX}/api:${IMAGE_TAG}` | FastAPI (uvicorn) | unless-stopped | none |
| `migrate` | same as `api` | One-shot `alembic upgrade head`; `api` waits for it to complete | no | none |
| `worker` | same as `api` | Celery worker (outbox relay) | unless-stopped | none |
| `beat` | same as `api` | Celery beat: `pbip.outbox.relay` every 10 s | unless-stopped | schedule file in `/tmp` |
| `db` | `postgres:16-alpine` | PostgreSQL 16; not published; TLS required for every TCP client (`tls-entrypoint.sh`) | unless-stopped | volume `pgdata`, `./certs/db` (read-only) |
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
| `PUBLIC_PORT` | web | Host HTTP port (default 80): redirects to HTTPS, serves `/.well-known/acme-challenge/` |
| `PUBLIC_HTTPS_PORT` | web | Host HTTPS port (default 443); also the port in the HTTP → HTTPS redirect |
| `PUBLIC_HOSTNAME` | deploy.sh | Host name in the placeholder certificate until certbot issues the real one (§1.4) |
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
- Metrics: `/metrics` on the API container (not proxied by nginx, which returns 404 for it). Monitoring stack:
  `docker compose --profile monitoring up -d` (`make up-all`); see §1.5 for dashboards, alerts, availability and
  error tracking.
- Audit trail: `GET /api/v1/compliance/audit-logs` and `GET /api/v1/compliance/audit-logs/verify` (compliance officer
  or admin, MFA session) — the verify endpoint recomputes the hash chain.

### 1.4 TLS (in the repository; certificate issuance is **External**)

TLS is configured in code (NFR-02). What runs where:

| Hop | Control | Where |
|---|---|---|
| Browser → `web` | nginx terminates TLS 1.2/1.3 (Mozilla intermediate ciphers, no session tickets) on 8443; port 8080 answers only `301` to `https://$host:${PUBLIC_HTTPS_PORT}` and ACME HTTP-01 challenges; HSTS `max-age=63072000; includeSubDomains` on static responses (nginx) and API responses (API, because `COOKIE_SECURE=true`) | `infra/nginx/default.conf.template`, `infra/nginx/security-headers.conf` |
| Refresh cookie | `Secure`, `HttpOnly`, `SameSite=Strict`; staging/production refuse to boot with `COOKIE_SECURE=false` | `core/config.py`, `modules/auth/router.py` |
| `api`/`worker`/`beat`/`migrate` → `db` | `sslmode=verify-full`: chain and host name `db` checked; PostgreSQL rejects plain TCP (`host ... reject`, only `hostssl` with SCRAM) | `DATABASE_URL` in the compose files, `infra/postgres/tls-entrypoint.sh` |
| `web` → `api`, services → `redis` | Plain TCP on the internal compose network, not published. Accepted: single host, no untrusted workloads on that network (threat model §1) | — |

**Staging host, first time** (needs DNS for `<host>` pointing at the host, ports 80 and 443 open, `PUBLIC_HOSTNAME`
set in `~/pbip/.env`):

1. Deploy normally. `deploy.sh` creates whatever is missing and never overwrites existing files:
   `certs/db/server.{crt,key}` (self-signed PostgreSQL certificate for host `db`, 10 years; the API pins it as its
   `sslrootcert`) and a 30-day placeholder `certs/web/tls.{crt,key}` for `PUBLIC_HOSTNAME`, so nginx can start and
   answer the ACME challenge. `certs/` is `0700`; the web key is `0644` inside it because nginx runs as uid 101.
2. Issue the real certificate with certbot's webroot plugin (nginx keeps serving; the challenge files go to
   `~/pbip/acme`, which `web` serves read-only):

   ```bash
   sudo apt-get install -y certbot
   sudo certbot certonly --webroot -w /home/<deploy-user>/pbip/acme -d <host> --agree-tos -m <ops-email> \
     --deploy-hook "PBIP_DIR=/home/<deploy-user>/pbip bash /home/<deploy-user>/pbip/certbot-deploy-hook.sh"
   ```

   `certbot-deploy-hook.sh` copies `fullchain.pem`/`privkey.pem` to `certs/web/tls.{crt,key}`, runs `nginx -t` and
   reloads nginx without downtime. certbot stores the hook in its renewal configuration, and the packaged
   `certbot.timer` renews automatically; check with `sudo certbot renew --dry-run`.
3. Verify from outside the host and record the output on the deployment issue (#198):

   ```bash
   curl -sI http://<host>/ | grep -iE '^(HTTP|location)'            # 301 to https://<host>:443/
   curl -sI https://<host>/api/health/live | grep -i strict-transport  # HSTS present
   python3 scripts/smoke_test.py https://<host>
   ```

**Rotating the database certificate:** delete `certs/db/*`, run the deploy again (new pair), then
`docker compose -f docker-compose.prod.yml up -d --force-recreate db api worker beat`. Clients pin the new
certificate as their root, so the old one stops working at once.

**Local and CI:** `python scripts/dev_certs.py` (run by `make setup`, `make up`, `make up-all` and `make infra-up`)
creates a throwaway local CA and leaf certificates in `infra/certs/` (git-ignored). The stack is at
`https://localhost:8443`; `http://localhost:8080` redirects. Browsers warn because they do not trust the local CA;
command-line clients pass `--cacert infra/certs/ca.crt` (curl) or `SSL_CERT_FILE=infra/certs/ca.crt` (Python; `make
smoke` does this). Do not import the dev CA into your browser: with HSTS, the browser would then refuse plain HTTP to
`localhost` for two years. `bash scripts/verify_tls.sh` proves the redirect, HSTS, TLS ≥ 1.2, that PostgreSQL rejects
plain TCP and that every application connection to it is encrypted; CI runs it in the e2e job.


### 1.5 Monitoring, alerts, availability and error tracking (TASK-069, TASK-103)

Everything is provisioned from files in the repository and runs only with the `monitoring` compose profile (off by
default: about 1 GB of RAM on the reference laptop). Self-hosted only; nothing is sent to a third party.

| Component | Config | What it gives you |
|---|---|---|
| Prometheus (35-day retention, volume `prometheus`) | `infra/prometheus/prometheus.yml`, `alerts.yml` | Scrapes the API, Redis, PostgreSQL, Celery and the blackbox probes every 15 s; evaluates the alert and recording rules |
| Grafana (folder "PBIP") | `infra/grafana/provisioning/`, `infra/grafana/dashboards/*.json` | Dashboards below; Prometheus and Loki datasources |
| blackbox-exporter | `infra/prometheus/blackbox.yml` | Availability probes every 15 s: `web-ready` = `https://web:8443/api/health/ready` through nginx TLS (chain verified), `api-ready` and `api-live` directly on the API |
| celery-exporter (worker runs with `-E`) | `docker-compose.yml` | Task outcomes, retries, runtimes, worker heartbeats |
| postgres-exporter, redis-exporter | `docker-compose.yml` | Connections, transactions, deadlocks, cache hit ratio, sizes; Redis memory, queue length |
| Loki + Alloy | `infra/loki/loki.yml`, `infra/alloy/config.alloy` | Every container's JSON logs, labelled `service` and `container`, searchable in Grafana Explore |

**Dashboards.** *PBIP - API latency and errors* (`pbip-api`): rate, 4xx/5xx ratio, p50/p95/p99 per route template,
unhandled errors and the error log panel. *PBIP - Fraud decisions* (`pbip-fraud`): ALLOW/STEP_UP/REVIEW/BLOCK per
minute and mix, non-ALLOW share, scoring latency, transfer outcomes, idempotent replays. *PBIP - Celery jobs and
PostgreSQL* (`pbip-platform`). *PBIP - Availability* (`pbip-availability`): last hour, 24 h and 30 days. *PBIP -
Overview* (unchanged).

**Alerts** (`infra/prometheus/alerts.yml`; state at http://localhost:9090/alerts). Page: `ApiHighErrorRate`,
`CeleryNoWorker`, `ScrapeTargetDown`, `AvailabilityBelow99`, `HealthProbeFailing`. Ticket: `ApiSlowP95`,
`ApiRouteSlowP95`, `FraudScoringSlow`, `FraudBlockRateHigh`, `LoginFailureSpike`, `CeleryBacklog`, `CeleryTaskFailures`,
`UnhandledErrors`, `PostgresDeadlocks`, `PostgresConnectionsHigh`. **No notification channel is configured**: there is
no Alertmanager, so alerts are visible in Prometheus and Grafana only. Choosing where pages go (email, chat) is an
owner decision for the staging host (#198).

**Availability figure (NFR "99% in the test environment").** `pbip:availability:ratio_30d{probe="web-ready"}` is the
fraction of 15-second readiness probes that succeeded (`web-ready` is the user path) over the last 30 days. It counts only time when the monitoring
stack itself was running, so stop it only deliberately. Report it monthly from the *Availability* dashboard.

**Error tracking.** Every unhandled exception produces one `unhandled_exception` log event with `request_id`,
`route` (template, never the raw path), `method`, `error_type` and `raised_at` (`file.py:line:function`), plus the
metric `unhandled_errors_total{route,error_type}`. The exception message, arguments and request body are never
logged, because they may contain personal data. The client sees only the generic 500 and the same `request_id`. To find
one: Grafana → Explore → Loki → `{service="api"} | json | event="unhandled_exception" | request_id="<id>"`.
The `UnhandledErrors` alert fires on the first occurrence.

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

PostgreSQL 16 runs in the `db` container with its data in the `pgdata` volume. Logical backups and restore drills are
automated by `scripts/backup.py` and `scripts/restore.py` (TASK-083); scheduling them, off-host storage and WAL
archiving are **External**. Redis holds only rate-limit counters and the Celery queue, which are safe to lose; its
AOF file in `redisdata` restores the queue after a restart.

**Where the scripts run.** They need a checkout of the repository at the deployed tag with `uv sync` done, `gpg`,
and either the PostgreSQL 16 client tools or access to the `docker` CLI (`--docker-container` runs `pg_dump`/
`pg_restore` inside the `db` container). They also connect with SQLAlchemy to take statistics and verify the audit
chain, using `DATABASE_URL` from the environment or `.env`. On the deployment host the database is not published, so
for the duration of a backup or drill publish it on loopback only with a host-kept override
(`docker-compose.prod.backup.yml`: `services: { db: { ports: ["127.0.0.1:5432:5432"] } }`, applied with
`docker compose -f docker-compose.prod.yml -f docker-compose.prod.backup.yml up -d db`) and point `DATABASE_URL` at
`127.0.0.1` (**External**: neither the override nor the checkout is installed by `deploy.sh`). Connections use
libpq's default `sslmode` (`prefer`); nothing in the scripts disables TLS.

### 4.1 Logical backup (daily, and before every migration or secret rotation)

```bash
cd ~/pbip-repo                      # checkout at the deployed tag, `uv sync --all-packages --frozen` done
set -a; . ~/pbip/.env; set +a
export DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/$POSTGRES_DB"
DB=$(docker compose -f ~/pbip/docker-compose.prod.yml ps -q db)
uv run python scripts/backup.py --out-dir /var/backups/pbip --passphrase-file /root/.pbip-backup-passphrase \
  --docker-container "$DB" --dry-run          # prints the plan, executes nothing
uv run python scripts/backup.py --out-dir /var/backups/pbip --passphrase-file /root/.pbip-backup-passphrase \
  --docker-container "$DB"                    # or --recipient <fingerprint> instead of --passphrase-file
```

What it does (`scripts/backup.py`):

1. Opens a `REPEATABLE READ` transaction and exports its snapshot (`pg_export_snapshot()`).
2. In that snapshot records: the row count of every table in every schema, the Alembic revision, the ledger totals
   (customer balance total, held total, net balance over all accounts including `SYSTEM`, which must be 0), a SHA-256
   over every account's `(id, ledger_balance, held_amount)`, the number of accounts whose balance differs from their
   entries and of transactions whose entries do not balance, the audit-log head (`seq`, `hash`) and the result of
   `AuditService.verify_chain` (`compliance/service.py`, the code behind `GET /api/v1/compliance/audit-logs/verify`).
3. Runs `pg_dump --format=custom --snapshot=<that snapshot>` piped straight into `gpg` (AES-256 symmetric, or
   public-key to `--recipient`), so the dump is exactly the state described in step 2 and no plaintext touches the
   disk. Output: `pbip-<db>-<UTC yyyymmddThhmmssZ>.dump.gpg`, mode 600; a failed run leaves no file behind.
4. Writes `pbip-<db>-<stamp>.manifest.json` (mode 600) next to it: the SHA-256 and size of the encrypted file plus the
   statistics from step 2. The manifest holds counts, totals and hashes only; no customer data, no credentials.

Exit codes: 0 success; 1 failure (nothing kept); 2 backup written but the **source** failed an integrity check (broken
audit chain, unreconciled account, unbalanced transaction): keep the file as evidence and open an incident.

- Copy both files off the host (object storage with versioning and a retention policy you choose; **External**).
  A backup that exists only on the host it protects is not a backup.
- Record in the backup log: file name, size, SHA-256 (from the script's output), the passphrase/key generation used,
  the `FIELD_ENCRYPTION_KEY` generation in use (§3.2 step 8) and the release tag. The dump contains the encrypted MFA
  secrets and phone numbers; restoring it needs the `FIELD_ENCRYPTION_KEY` of that time as well as the backup key.

#### 4.1.1 Backup key management

- **Symmetric (`--passphrase-file`).** Generate once per key generation:
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))" > /root/.pbip-backup-passphrase` then
  `chmod 600 /root/.pbip-backup-passphrase`. The script refuses a passphrase file that group or others can read, and
  gpg reads the file itself (`--passphrase-file`, `--pinentry-mode loopback`, `--no-symkey-cache`), so the passphrase
  never appears in a command line, an environment variable or a log. Keep a copy in the secret store under two-person
  control; never store it with the dumps or in the off-host bucket.
- **Public key (`--recipient`, preferred for production).** The host holds only the public key, so a compromised host
  cannot read earlier backups. Create the key pair offline, import the public key on the host
  (`gpg --import pbip-backup.pub`) and mark it trusted (`echo "<FINGERPRINT>:6:" | gpg --import-ownertrust`; without
  it gpg refuses to encrypt in batch mode). The private key lives in the secret store and is imported only on the
  machine that runs the restore drill; `scripts/restore.py --passphrase-file` then unlocks that private key, or gpg
  prompts for it.
- **Rotation.** A new passphrase or key applies to new backups only. Keep each old passphrase/private key for as long as
  any backup encrypted with it is retained, and record which generation encrypted which backup. A lost key is a lost
  backup: the monthly drill (§4.2) proves the stored copy still decrypts.

### 4.2 Restore drill (monthly) and integrity check

```bash
uv run python scripts/restore.py /var/backups/pbip/pbip-<db>-<stamp>.dump.gpg \
  --passphrase-file /root/.pbip-backup-passphrase --docker-container "$DB" --dry-run
uv run python scripts/restore.py /var/backups/pbip/pbip-<db>-<stamp>.dump.gpg \
  --passphrase-file /root/.pbip-backup-passphrase --docker-container "$DB"
```

`scripts/restore.py` (same environment as §4.1; the manifest must sit next to the dump) never writes to the live
database. It:

1. checks the encrypted file's SHA-256 and size against the manifest and stops before decrypting anything if they
   differ;
2. creates a new database `pbip_restore_drill_<stamp>_<random>` on the same server and streams
   `gpg --decrypt | pg_restore --no-owner --exit-on-error` into it. gpg's own integrity check fails a tampered file
   even if its manifest was edited to match;
3. compares the restored copy with the manifest: the same tables, every table's row count, the Alembic revision,
   every ledger figure (including the per-account balance hash) and the audit head (`seq`, `hash`);
4. checks the copy on its own: `AuditService.verify_chain` from genesis must be valid and cover every `audit_logs`
   row (hash-chain continuity: nothing lost, reordered or altered), every account balance must equal its entries,
   every transaction must balance and the net balance must be 0;
5. drops the drill database (`--keep` leaves it for investigation; drop it afterwards with
   `DROP DATABASE <name> WITH (FORCE)`).

It prints a JSON report (`PASS`/`FAIL`, problems, timings, the restored counts, ledger and audit figures) and exits 1
on any mismatch. Attach the report to the operations issue. A restore that was never drilled does not count as a
recovery capability. A `FAIL` means the backup cannot be trusted: keep the files, take a fresh backup, investigate.

**Drill record (local stack, 2026-11-01).** Not staging: no staging host exists yet (TASK-082). Machine: developer
laptop, Intel Celeron N4500 (2 cores, 1.1 GHz), 7 GiB RAM, PostgreSQL 16.15 in `postgres:16-alpine`, gpg 2.4.8,
tools run with `--docker-container`. Source: the local `pbip` database with demo and synthetic data.

| | Symmetric AES-256 | Public key (ed25519/cv25519) |
|---|---|---|
| Encrypted dump | 12,314,473 bytes | 12,311,374 bytes |
| Backup (snapshot statistics + dump + encrypt) | 15.0 s | 8.0 s |
| Decrypt + `pg_restore` | 13.5 s | 13.0 s |
| Verification (counts, ledger, chain) | 0.5 s | 0.8 s |
| Result | PASS | PASS |

Restored copy, identical to the manifest in both runs: 39 tables, 775,226 rows (including `synthetic.transactions`
772,079, `public.transactions` 46, `public.transaction_entries` 86, `public.accounts` 45, `public.audit_logs` 151,
`public.security_events` 241); Alembic revision `0009`; customer balance total 170,000,000 minor units
(₹17,00,000.00), held 11,000,000, net balance 0, 0 unreconciled accounts, 0 unbalanced transactions; audit chain valid,
151 of 151 rows verified, head `seq` 151. Negative runs on the same backup, all `FAIL` with exit 1 and no drill
database left behind: one flipped byte (checksum mismatch, nothing decrypted), the wrong passphrase (gpg exit 2), and
a flipped byte with the manifest's SHA-256 rewritten to match (gpg integrity failure, exit 2).

**Manual fallback** (if the scripts cannot run). Back up with
`docker compose -f docker-compose.prod.yml exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc | gpg --symmetric --cipher-algo AES256 --batch --pinentry-mode loopback --passphrase-file /root/.pbip-backup-passphrase > /var/backups/pbip/pbip-$(date -u +%Y%m%dT%H%M%SZ).dump.gpg`
and restore into a scratch database with
`gpg --decrypt --batch --pinentry-mode loopback --passphrase-file /root/.pbip-backup-passphrase "$F" | docker compose -f docker-compose.prod.yml exec -T db pg_restore -U "$POSTGRES_USER" -d <database> --no-owner --exit-on-error`.
Then verify by hand:

1. Row counts of the core tables match the source:
   `SELECT 'users', count(*) FROM users UNION ALL SELECT 'accounts', count(*) FROM accounts UNION ALL SELECT 'transactions', count(*) FROM transactions UNION ALL SELECT 'transaction_entries', count(*) FROM transaction_entries UNION ALL SELECT 'audit_logs', count(*) FROM audit_logs;`
2. The ledger balances: `SELECT count(*) FROM accounts a WHERE a.ledger_balance <> (SELECT coalesce(sum(CASE WHEN direction='CREDIT' THEN amount ELSE -amount END),0) FROM transaction_entries e WHERE e.account_id=a.id);` must return `0`.
3. The audit hash chain, with the API's own verifier:

   ```bash
   docker compose -f docker-compose.prod.yml exec \
     -e DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/<database>" \
     api python -c "from pbip_api.core.db import get_engine; from sqlalchemy.orm import Session; from pbip_api.modules.compliance.service import AuditService; s = AuditService(Session(get_engine())).verify_chain(); print(s); raise SystemExit(0 if s.valid else 1)"
   ```

   `valid=True` with `checked` equal to the `audit_logs` row count; `valid=False` names the first bad `seq`.

### 4.3 Full restore of the live database

Only after the decision in §5 (or a confirmed data loss), with the team informed:

1. `docker compose -f docker-compose.prod.yml stop web api worker beat` (keep `db` and `redis` up).
2. Take a safety dump of the current state as in §4.1 (even if it is believed corrupt), and run the §4.2 drill on
   the backup you are about to restore: only a backup that drills `PASS` goes back into production.
3. Recreate: `... psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$POSTGRES_DB\";" -c "CREATE DATABASE \"$POSTGRES_DB\";"` then `pg_restore` into it with the manual command in §4.2 (`-d "$POSTGRES_DB"`).
4. `docker compose -f docker-compose.prod.yml up -d` (runs `migrate`, which is a no-op if the backup is at head, or
   applies migrations released after the backup).
5. Run the §4.2 manual checks 1–3 against the live database and the smoke test, then
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

**Facts that shape the response** (from the code; threat model §5): every request with an access JWT checks the
user's `status` and `token_version` in the database (ADR-0021, `auth/dependencies.py`), so disabling a user stops
login, refresh **and** their outstanding access tokens on the next request. Refresh tokens live in `refresh_tokens` and can be revoked server-side. Staff routes also
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
   Login, refresh **and every request with an existing access token** now fail for this user (`UserStatus.DISABLED`
   is checked on all three paths; access tokens since ADR-0021). Confirm:
   `SELECT status FROM users WHERE id='<user_id>'; SELECT count(*) FROM refresh_tokens WHERE user_id='<user_id>' AND revoked_at IS NULL;` → `DISABLED`, `0`.
3. **Confirm the session is dead.** A request with the attacker's token now returns 401 (ADR-0021). Watch the audit log
   for anything the actor did before containment: `SELECT seq, created_at, action, resource_type, resource_id FROM audit_logs WHERE actor_user_id='<user_id>' AND created_at > now() - interval '30 minutes' ORDER BY seq;`
4. **Kill switch** (only if the database check itself is in doubt, for example the users table was tampered with):
   rotate `JWT_SECRET` now (§3.1). This invalidates *every* access token platform-wide within one rollout;
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
- [ ] §4.1 backup produced with `scripts/backup.py` and copied off-host; §4.2 `scripts/restore.py` drill reports `PASS`
      on the real host (done so far only on the local stack, §4.2 drill record).
- [ ] §4.4 only if WAL archiving is configured: a PITR to a chosen timestamp drilled on the real host.
- [ ] §5.2 steps 1–2 rehearsed on staging with a test staff user; `DISABLED` user cannot refresh or log in.
- [ ] `apps/api/tests/unit/test_runbook_references.py` passes (every file, `make` target, API path and variable this
      document cites still exists).

None of the staging/production checks above has been run yet as of the first version of this runbook (no
staging host exists; TASK-082 creates it). The repository-level checks (script tests, reference test) run in CI.
