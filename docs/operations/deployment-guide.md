# PBIP deployment guide

**Audience:** whoever ships a PBIP release: the release owner, and the platform engineer who looks after the staging
host. **Scope:** this is the entry point. It says what has to be in place, how a release travels from a tag on `main`
to the staging host, how to roll it back, and how the two model files a stack can serve are produced. The detailed
procedures live in two documents, and this guide points into them rather than repeating them:

* the [runbook](runbook.md): every day-2 operation on a running stack (deploy, roll back, restart, secret rotation,
  backup and restore, incident playbooks);
* the [Oracle Cloud Arm staging guide](deploy-oracle-cloud-arm.md): building the staging host itself, from the cloud
  account to the first deployment.

The [deployment diagrams](../diagrams/deployment.md) show both targets. Never paste secrets, tokens or customer data
into issues, chat or documents.

---

## 1. Targets

| Target | Compose file | Who uses it | Data |
|---|---|---|---|
| Local stack | `docker-compose.yml` (`make up`, `make up-all`) | Developers and the live demo, at https://localhost:8443 | Demo data from `make seed` |
| Staging | `infra/deploy/docker-compose.prod.yml`, project `pbip-prod`, on one Oracle Cloud Always Free Arm VM (`https://pbip.duckdns.org`, 2 OCPUs, 12 GB, since 8 Nov 2026) | Testers; release verification | Empty: customers register themselves. `scripts/seed_dev.py` refuses `APP_ENV=staging` |

There is no production deployment. The staging compose is production-like: images pulled from GHCR, no build on the
host, PostgreSQL and Redis not published, two API replicas, TLS at nginx, and the API refusing to boot with unsafe
configuration (`APP_ENV=staging`, runbook section 1.2).

## 2. Prerequisites

**For any release**

* The commit to release is on `main`, and `CI Gate` is green for it (the ruleset requires it for every merge).
* The release notes and CHANGELOG entry are merged (`docs/release/`, `CHANGELOG.md`).
* A database backup of staging taken in the last hour when the release contains a migration (runbook section 4.1).

**For the staging host** (one-time; Oracle guide sections 2 to 11)

* The VM with Docker Engine and the Compose plugin, the `pbip` deploy user, SSH hardened, host firewall set.
* A host name with a Let's Encrypt certificate (option A, needed for deploys from GitHub) or the IP address with a
  private CA (option B, manual deploys only).
* `~/pbip/.env` created from `infra/deploy/.env.prod.example`, with generated secrets (runbook section 1.2).
* The host logged in to GHCR with a read-only `read:packages` token (Oracle guide section 10).
* Release images for `linux/arm64`: the `package` job builds `linux/amd64` and `linux/arm64`.

**For deploys from GitHub Actions** (optional; Oracle guide section 13)

* GitHub environment `staging` with the secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`
  and the variable `STAGING_URL`; the repository variable `DEPLOY_ENABLED=true`.
* Port 22 reachable from GitHub-hosted runners for the duration of the run (they have no fixed address).

## 3. Release path: tag, Deploy workflow, staging

```text
main (CI Gate green) --tag vX.Y.Z--> Deploy workflow: verify -> package -> deploy (only if DEPLOY_ENABLED=true)
                                                                      |
                                       staging host: deploy.sh deploy -> health checks -> smoke test
                                                                      |
                                                    on failure: deploy.sh rollback (automatic)
```

1. **Tag.** From an up-to-date `main`: `git tag vX.Y.Z && git push origin vX.Y.Z`. Use semantic versions; the
   workflow accepts only `vX.Y.Z`.
2. **verify** (`.github/workflows/deploy.yml`): the tag has the right format and its commit is on `main`.
3. **package:** builds the `api` and `web` images for `linux/amd64` and `linux/arm64` and pushes them to GHCR as
   `ghcr.io/<owner>/pbip/api:vX.Y.Z` and `.../web:vX.Y.Z`. Images are immutable per tag.
4. **deploy** (skipped unless `DEPLOY_ENABLED=true`; one deployment at a time): checks that the secrets and variables
   exist (names only), connects over SSH with a pinned host key, copies the compose file, `deploy.sh` and the TLS
   helpers to `~/pbip`, and runs `bash deploy.sh deploy <prefix> vX.Y.Z`. The script pulls the images, runs `migrate`
   (`alembic upgrade head`) before the API starts, starts `web` only after the API is healthy, and records the tag
   in `~/pbip/.deployed_tag` only once the whole stack is healthy (the previous one moves to `.previous_tag`).
5. **Smoke test:** the runner runs `scripts/smoke_test.sh` against `STAGING_URL` (readiness and security headers).
6. **Automatic rollback:** if the deploy or the smoke test fails, the workflow runs `bash deploy.sh rollback` on the
   host and the run is marked failed.

**Dry run.** Actions, Deploy, Run workflow with the tag and `dry_run = true`: verifies the tag, builds the images,
checks the secrets and prints the plan without touching the host.

**Manual deploy.** With `DEPLOY_ENABLED` unset, a tag push still verifies and packages; the owner then deploys from a
laptop over SSH with the same `deploy.sh` (Oracle guide section 12). Both paths end in the same health-gated start.

**After a deploy**, record on the release issue: the run URL, `docker compose -f docker-compose.prod.yml ps` (with
`migrate` exited 0), `cat ~/pbip/.deployed_tag`, and the output of `python3 scripts/smoke_test.py https://<host>` and
`python3 scripts/staging_smoke.py` (runbook section 2.0).

**Cutover.** Containers are recreated in place on one host: a health-gated, minimal-downtime deployment, not a
zero-downtime one (runbook section 2.1). Staging runs two API replicas (ADR-0045); the availability probe measures any
downtime during the window rather than assuming none (Oracle guide section 17).

## 4. Rollback

| How | Command | When |
|---|---|---|
| Automatic | The Deploy workflow runs `deploy.sh rollback` | A deploy or smoke test failed |
| From GitHub | Actions, Deploy, Run workflow with `tag = v<previous>` | Redeploy any earlier release through the same health-gated path |
| On the host | `cd ~/pbip && bash deploy.sh rollback` | Restart the release recorded in `.previous_tag` |

**Migrations are not rolled back automatically.** Every migration has a tested downgrade (CI runs up, down, up), but
downgrade only when the previous release cannot run on the new schema, and take a backup first (runbook section 2.2).
Data written by the newer release may not survive a downgrade; restoring a backup is the last resort (runbook
section 4.3).

## 5. Model files

The API serves two optional models. Each is a directory of files mounted read-only into the `api` container and loaded
once at start-up, never on a request. Without them the API serves its rule-based fallbacks, so a missing model never
stops a deployment.

| Model | Setting | Without the files | Local stack mount |
|---|---|---|---|
| Hybrid fraud scorer (LightGBM, Isolation Forest, rules floor; ADR-0019) | `FRAUD_SCORER=hybrid`, `FRAUD_MODEL_DIR` | Rules only, marked `+rules-only` with a `MODEL_UNAVAILABLE` reason; never allow-all | `./models/fraud` to `/models/fraud` |
| Recommendation ranker trained on simulated uptake (ADR-0053) | `RECOMMENDATION_MODEL_DIR` | `rules-v1` ranking | `./models/recommendations` to `/models/recommendations` |

`docker-compose.yml` sets both (`FRAUD_SCORER` defaults to `hybrid` there). The staging compose sets neither by
default, and the code default is `FRAUD_SCORER=rules`: **staging serves the baseline fraud rules and `rules-v1`
recommendations** unless the host `.env` says `ML_MODELS=enabled`. Then `deploy.sh` adds
`infra/deploy/docker-compose.prod.models.yml`, which sets both and mounts `~/pbip/models/fraud` and
`~/pbip/models/recommendations` read-only, and refuses to deploy if the model files are missing (ADR-0061; procedure
in the [staging guide](deploy-oracle-cloud-arm.md) section 14.1).

### 5.1 The pinned fraud model: `make fraud-model-release`

```bash
make fraud-model-release      # no training, no MLflow needed
```

Downloads the release bundle named in `ml/models/fraud-champion.lock.json` (at present `hybrid-lgbm-iforest-3`,
published as an asset of the v1.0.0 GitHub release), checks its SHA-256 and its manifest's SHA-256 against the lock
file, and installs it into `models/fraud`. A bundle that does not match the lock is refused. Restart the API
afterwards (`docker compose up -d api`) so it loads the files.

Where the pinned model comes from, for the ML owner: `make fraud-model` trains, `make promote-model` measures the run
and promotes it to the MLflow `champion` alias only if it passes the gate, `make fraud-champion` exports the champion
with checksums, and `make fraud-bundle` packs `models/fraud` into a byte-reproducible bundle for a GitHub release. A new
pinned model is a pull request that changes the lock file, with the evaluation report.

### 5.2 The recommendation model: `make recommendation-model`

```bash
make recommendation-model
```

Generates the simulated uptake panel (data-gen `uptake`, seed 42, 3,000 customers over 24 months) and a perturbed
held-out variant (seed 1042), trains the logistic regression and the LightGBM ranker, and gates them against `rules-v1`
(ADR-0053). The model files are written into `models/recommendations` **only if the gate passes**; `report.json` is
always written and says why. Restart the API afterwards. The labels are simulated, so the gate shows the ranker beats
the rules on simulated data only, and the API's responses say so.

## 6. Checklist

- [ ] `main` green, release notes merged, tag `vX.Y.Z` pushed.
- [ ] Staging backup taken if the release has a migration.
- [ ] Deploy workflow green (or the manual deploy finished), `.deployed_tag` shows the new tag.
- [ ] Smoke tests pass; evidence recorded on the release issue.
- [ ] On the demo machine: `make fraud-model-release` (and `make recommendation-model` if the ranker is wanted), then
      `docker compose up -d api`.
- [ ] Rollback path known: previous tag recorded in `.previous_tag`.

## 7. Related documents

* [Runbook](runbook.md), [Oracle Cloud Arm staging guide](deploy-oracle-cloud-arm.md),
  [support guide](support-guide.md), [private-repository checklist](private-repo-checklist.md).
* [Setup guide](../SETUP_GUIDE.md) for the local stack.
* [Deployment diagrams](../diagrams/deployment.md).
* ADRs: [0019 hybrid fraud scorer serving](../adr/0019-hybrid-fraud-scorer-serving.md),
  [0045 API replicas](../adr/0045-api-scales-with-replicas.md),
  [0053 recommendation ranker gate and serving](../adr/0053-simulated-uptake-v2-ranker-gate-and-serving.md).
