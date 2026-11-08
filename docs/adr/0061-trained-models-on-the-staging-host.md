# ADR 0061: Trained models on the staging host, as an opt-in compose overlay with operator-copied files
- Status: Accepted (under owner's standing approval, 2026-11-08)
- Date: 2026-11-08
- Deciders: Architecture Team (follows ADR-0019, ADR-0033, ADR-0049, ADR-0053, ADR-0059)

## Context
The local demo stack (`docker-compose.yml`) serves the hybrid fraud scorer (`FRAUD_SCORER=hybrid`,
`FRAUD_MODEL_DIR=/models/fraud`, ADR-0019) and the gated recommendation ranker
(`RECOMMENDATION_MODEL_DIR=/models/recommendations`, ADR-0049, ADR-0053) from `./models/...`, mounted read-only
(threat model T34). The staging host (Oracle Cloud A1, linux/arm64) runs `infra/deploy/docker-compose.prod.yml`,
which sets neither and mounts nothing, so staging serves the fraud rules and `rules-v1` only. The owner wants
staging able to run the same models. The API image already contains the inference code and LightGBM's OpenMP
runtime for arm64; what is missing is the files on the host and the settings.

Constraints: the default deployment must not change; the model files are large artefacts (one is a pickle) and
must not come from a request path, from CI or from a writable location; a misconfiguration must not silently
serve the rules while the operator believes the models are on; a release and its rollback must use the same
configuration.

## Decision
1. **`infra/deploy/docker-compose.prod.models.yml`, a `-f` overlay of the `pbip-prod` project.** It adds to `api`
   `FRAUD_SCORER=hybrid`, `FRAUD_MODEL_DIR`, `RECOMMENDATION_MODEL_DIR`, `FRAUD_MODEL_BUDGET_MS` (from `.env`,
   default 50) and two read-only bind mounts, `./models/fraud` and `./models/recommendations`. The `worker` gets
   the fraud settings and the fraud mount only: it scores scheduled transfers with the same engine as a request
   (`payments/scheduled_service.py`) and ranks nothing. `migrate` and `beat` load no model and get nothing. Unlike
   the monitoring stack (ADR-0059), these are settings of the release's own services, so an overlay in the same
   project is right: `up --remove-orphans` and the health gate apply unchanged.
2. **Opt-in through `ML_MODELS=enabled` in the host `.env`.** `deploy.sh` adds the overlay to the compose file set
   for `deploy` and `rollback` alike. With the flag on, it refuses (exit 5, before any state file or container
   changes) when `models/fraud/manifest.json` or `models/recommendations` is missing, or when `ML_MODELS` has a
   value other than `enabled`, `disabled` or empty. Without the check, Docker would create the missing directory
   empty (owned by root) and the API would log `*_model_not_loaded` and serve the rules, which looks healthy.
   The monitoring path (`deploy.sh monitoring`) is not affected.
3. **The files are operator-supplied.** The operator builds them on the laptop (`make fraud-model-release`, the
   pinned and checksummed champion, or `make fraud-model`; `make recommendation-model`, which writes the ranker
   only if it passes its gate) and copies them with `scp` to `~/pbip/models/`, owned by the deploy user. The
   deploy job copies the overlay file with the other compose files, never the models.
4. **Fail-safe is unchanged.** Any load problem logs `fraud_model_not_loaded` / `recommendation_model_not_loaded`
   and the rules decide (fraud decisions marked `+rules-only`; never allow-all). The rules remain a floor the
   model cannot lower (ADR-0019). The model step keeps its budget (`FRAUD_MODEL_BUDGET_MS`), measured on the host
   with `scripts/benchmark_fraud_latency.py` (staging guide section 14.1).

## Alternatives considered
* **Bake the models into the API image** - every model change would need a new release and image; the image
  would carry a pickle whose provenance is the CI runner, and the image could no longer run rules-only by simply
  omitting a mount. The fraud model is already pinned and checksummed separately (`fraud-champion.lock.json`),
  so it does not need the image as its delivery channel.
* **Always-on mounts in `docker-compose.prod.yml`** - changes the default for every deployment, and on a host
  without the files Docker creates empty root-owned directories and the API quietly serves the rules; the
  opt-in flag plus the refusal makes "on" an explicit, checked decision.
* **Setting `FRAUD_SCORER` and the paths directly in the host `.env`** - the production compose file does not
  pass them, and it would still need the mounts; a reviewed overlay keeps the configuration in the repository.

## Consequences
* Staging can serve the same models as the demo with three host steps (copy, flag, redeploy) and turn them off
  with one (`ML_MODELS=disabled`, redeploy). Default deployments are byte-for-byte unchanged.
* Hand-run `docker compose ... up` commands on the host must name both files while the models are on (runbook
  section 2.3); `deploy.sh` does it by itself.
* A rollback with the flag on also needs the model files; if they are gone, the operator disables the flag first.
* Security: the models are code-adjacent artefacts (the Isolation Forest is a pickle, read by the allow-listed
  unpickler of ADR-0033 after a SHA-256 manifest check). They are mounted read-only, and the host files belong to
  the deploy user while the containers run as the image's `app` user, so a compromised container cannot rewrite
  what it loads. Writing `~/pbip/models` requires the deploy user's shell, which already controls `.env` and the
  compose files. This is the case threat model T34 describes ("the directory must be read-only to the API
  user"); no new threat row is needed. No data leaves the stack and nothing new is published.
* Standing-approval conditions: no weakening of security, auth, RBAC or audit (read-only mounts, the existing
  manifest and unpickler checks and the rules floor apply; the default stays rules-only); no money path (the
  ledger is untouched; the scorer only decides, as on the local stack); no migration; no new external service,
  paid dependency or data sent outside the stack (files are copied by the operator over the existing SSH); no
  fake data or metrics (the recommendation ranker is trained on simulated uptake and the API already says so,
  ADR-0053); within the owner's requested scope.
