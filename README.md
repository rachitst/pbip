# PBIP - Personal Banking Intelligence Platform

A simulated retail bank with a fraud, analytics and AI layer, engineered like a regulated financial product:
double-entry ledger, idempotent payments, MFA, object-level authorization, explainable fraud decisions,
tamper-evident audit trail, observability and a full CI/CD pipeline. (No real money; synthetic data only.)

* **Start here:** [`docs/PROJECT_BLUEPRINT.md`](docs/PROJECT_BLUEPRINT.md) - architecture, plan, scope, demo.
* **Complete Setup Guide (Ubuntu & Windows):** [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md) - zero-assumption setup, native PowerShell, Docker, troubleshooting.
* **Working with AI assistants:** [`AGENTS.md`](AGENTS.md). **Contributing:** [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Quick start (about 10 minutes)

PBIP supports **native Ubuntu Linux** (via `make` or `python scripts/dev.py`) and **native Windows 10/11** (via PowerShell `.\dev.ps1` or `dev.cmd`) without requiring WSL2. For full setup instructions, see [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md).

### 0. Install prerequisites
| Tool | Native Windows | Ubuntu / Debian | macOS |
|---|---|---|---|
| Git | `winget install Git.Git` | `sudo apt install git` | `brew install git` |
| uv (Python) | `winget install Astral-sh.uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `brew install uv` |
| Node.js 22 | `winget install OpenJS.NodeJS.LTS` | `curl -fsSL https://deb.nodesource.com/setup_22.x \| sudo bash -` | `brew install node@22` |
| Docker | Docker Desktop | Docker Engine / Desktop | Docker Desktop |
| GitHub CLI | `winget install GitHub.cli` | `sudo apt install gh` | `brew install gh` |

### 1. Run it
```bash
make setup        # .env with generated secrets, Python + Node deps, git hooks
make infra-up     # PostgreSQL + Redis in Docker
make migrate      # create the schema
make seed         # demo users, funded accounts, MFA-enrolled staff (local DB only)
make dev-api      # terminal 1 -> http://localhost:8000/docs
make dev-web      # terminal 2 -> http://localhost:5173
```
Or the whole stack in Docker: `make up` -> http://localhost:8080 (add `make up-all` for Grafana :3000,
Prometheus :9090, MLflow :5000). Demo logins: `alice@demo.pbip` / `bob@demo.pbip`; the password is the `DEMO_PASSWORD` constant in
`scripts/seed_dev.py` (never printed). Staff TOTP codes: `uv run python scripts/totp.py analyst@demo.pbip`
(decrypts the MFA secret from the local database; no secrets file is written).

### 2. Prove it works
```bash
make check                                   # lint + types + boundaries + security + all tests (what CI runs)
uv run python demo/demo_scenarios.py idempotency       # 10 identical requests -> 1 transfer
uv run python demo/demo_scenarios.py fraud-new-payee   # large transfer to a new payee is held
uv run python demo/demo_scenarios.py bola              # Bob cannot read Alice's account (404)
```

## Daily workflow
`git switch -c feat/short-name` -> code + tests -> `make check` -> `git commit -m "feat(scope): ..."` ->
`git push -u origin HEAD` -> open PR -> peer review -> CI Gate green -> squash-merge.
Useful: `make help`, `make openapi` (after API changes), `make test-unit` (no DB), `make infra-up` before `make test`.

Repository configuration (labels, sprint milestones, ruleset) can be initialized or updated via:
`python scripts/setup_github.py <OWNER>/<REPO> [--dry-run]`

## Layout
`apps/api` FastAPI modular monolith - `apps/web` React/TypeScript - `ml` ML contracts/features/inference -
`data-gen` synthetic data - `infra` nginx/Prometheus/Grafana/Loki/deploy - `docs` blueprint, ADRs, security -
`scripts` seed/OpenAPI/smoke/GitHub setup - `demo` live scenarios.

## Deploying (staging VM)
Push a `vX.Y.Z` tag from `main` (`git tag v0.1.0 && git push origin v0.1.0`): the workflow verifies the tag is on
`main`, builds images to GHCR and, when repository variable `DEPLOY_ENABLED=true`, deploys over SSH with a
health-gated start, runs `scripts/smoke_test.sh` and rolls back automatically on failure. Required secrets:
`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`; variable `STAGING_URL`. Server `.env`:
`infra/deploy/.env.prod.example`. Rollback, secret rotation, backup/restore and incident playbooks:
`docs/operations/runbook.md`.

## Troubleshooting
* `make: command not found` on Windows -> use native PowerShell wrapper `.\dev.ps1 <command>` or `dev.cmd <command>` (see `docs/SETUP_GUIDE.md`).
* Tests skip with "run `make infra-up`" -> PostgreSQL is not running (in CI a missing DB is a hard failure).
* `NoReferencedTableError` -> a new `models.py` was not added to `pbip_api/modules/__init__.py` and `alembic/env.py`.
* `alembic check` fails -> model changed without a migration: `alembic revision --autogenerate`.
* Port in use (5432/6379/8000/8080) -> stop the other service or edit the published ports in `docker-compose.yml`.
