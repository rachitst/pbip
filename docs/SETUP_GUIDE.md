# PBIP Local Environment Setup & Contributor Guide

A comprehensive, zero-assumption setup guide for developers working on the **Personal Banking Intelligence Platform (PBIP)**.

PBIP fully supports **native Ubuntu Linux** (via `make` or `python scripts/dev.py`) and **native Windows 10/11** (via PowerShell `.\dev.ps1` or `dev.cmd`) without requiring WSL2.

---

## Table of Contents
1. [Architecture & Stack Overview](#1-architecture--stack-overview)
2. [Quick Reference: Command Equivalents](#2-quick-reference-command-equivalents)
3. [Native Ubuntu Setup (Step-by-Step)](#3-native-ubuntu-setup-step-by-step)
4. [Native Windows Setup (Step-by-Step)](#4-native-windows-setup-step-by-step)
5. [Starting Services & Local Development](#5-starting-services--local-development)
6. [Running in Full Docker Stack](#6-running-in-full-docker-stack)
7. [Running Tests, Linting & Verification](#7-running-tests-linting--verification)
8. [URLs, Ports, Health Checks & Log Access](#8-urls-ports-health-checks--log-access)
9. [Stopping Services & Workspace Cleanliness](#9-stopping-services--workspace-cleanliness)
10. [GitHub CLI Setup & Project Automation](#10-github-cli-setup--project-automation)
11. [Team Contributor Onboarding](#11-team-contributor-onboarding)
12. [Common Troubleshooting & FAQ](#12-common-troubleshooting--faq)

---

## 1. Architecture & Stack Overview

PBIP is engineered as a regulated banking system:
- **API (`apps/api`):** FastAPI modular monolith (Python 3.12, SQLAlchemy 2 async, Alembic, Celery, Redis, argon2id).
- **Web Frontend (`apps/web`):** React 19 + TypeScript + Vite, Tailwind CSS / Vanilla CSS components.
- **ML & Analytics (`ml/`, `data-gen/`):** Isolation contracts, synthetic customer generation, LightGBM fraud models, Optuna, MLflow.
- **Infrastructure (`infra/`):** PostgreSQL 16, Redis 7, Prometheus, Grafana, Loki, Nginx reverse proxy.

---

## 2. Quick Reference: Command Equivalents

PBIP uses a single unified runner (`scripts/dev.py`). You can invoke it natively on any OS:

| Operation | Ubuntu / macOS (`make`) | Windows PowerShell (`dev.ps1`) | Direct Python (Cross-Platform) |
|---|---|---|---|
| **Environment Doctor** | `make doctor` | `.\dev.ps1 doctor` | `python scripts/dev.py doctor` |
| **First-Time Setup** | `make setup` | `.\dev.ps1 setup` | `python scripts/dev.py setup` |
| **Start DB & Redis** | `make infra-up` | `.\dev.ps1 infra-up` | `python scripts/dev.py infra-up` |
| **Stop DB & Redis** | `make infra-down` | `.\dev.ps1 infra-down` | `python scripts/dev.py infra-down` |
| **Run Migrations** | `make migrate` | `.\dev.ps1 migrate` | `python scripts/dev.py migrate` |
| **Seed Demo Data** | `make seed` | `.\dev.ps1 seed` | `python scripts/dev.py seed` |
| **Start API** | `make dev-api` | `.\dev.ps1 dev-api` | `python scripts/dev.py dev-api` |
| **Start Frontend** | `make dev-web` | `.\dev.ps1 dev-web` | `python scripts/dev.py dev-web` |
| **Start Celery Worker** | `make dev-worker` | `.\dev.ps1 dev-worker` | `python scripts/dev.py dev-worker` |
| **Run All CI Checks** | `make check` | `.\dev.ps1 check` | `python scripts/dev.py check` |
| **Run Unit Tests** | `make test-unit` | `.\dev.ps1 test-unit` | `python scripts/dev.py test-unit` |
| **Run Web Tests** | `make test-web` | `.\dev.ps1 test-web` | `python scripts/dev.py test-web` |
| **Regen OpenAPI Contract** | `make openapi` | `.\dev.ps1 openapi` | `python scripts/dev.py openapi` |
| **Run Post-Deploy Smoke** | `make smoke` | `.\dev.ps1 smoke` | `python scripts/dev.py smoke` |
| **Clean Temp Artifacts** | `make clean` | `.\dev.ps1 clean` | `python scripts/dev.py clean` |

---

## 3. Native Ubuntu Setup (Step-by-Step)

### 3.1 Install System Prerequisites
Open a terminal and run:
```bash
sudo apt update
sudo apt install -y git curl build-essential libpq-dev
```

### 3.2 Install `uv` (Fast Python Package Manager)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.cargo/env  # or restart your terminal
uv --version         # Verify: uv 0.4+ recommended
```

### 3.3 Install Node.js (v20+ or v22 LTS)
```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version       # Verify: v20.x or v22.x
npm --version
```

### 3.4 Install Docker Engine & Docker Compose
```bash
# Follow official Docker instructions or install docker.io:
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# Log out and log back in for docker group changes to take effect
docker info          # Verify Docker daemon is running
```

### 3.5 Install GitHub CLI (Optional but recommended)
```bash
sudo apt install -y gh
gh auth login        # Authenticate with your GitHub account
```

### 3.6 Clone Repository & First-Time Setup
```bash
git clone https://github.com/<your-org-or-username>/pbip.git
cd pbip

# Run the automated doctor check
make doctor

# Perform complete workspace setup
make setup
```
`make setup` automatically:
1. Creates `.env` from `.env.example` with cryptographically secure generated keys (`PBIP_SECRET_KEY`, `POSTGRES_PASSWORD`, etc.) if not present.
2. Installs Python dependencies via `uv sync --all-packages`.
3. Installs frontend dependencies via `npm ci` in `apps/web`.
4. Installs git pre-commit hooks via `uv run pre-commit install`.

---

## 4. Native Windows Setup (Step-by-Step)

PBIP runs natively on Windows PowerShell without requiring WSL.

### 4.1 Install Windows Prerequisites
Open PowerShell as **Administrator** and install the tooling using `winget`:
```powershell
winget install --id Git.Git -e --source winget
winget install --id Astral-sh.uv -e --source winget
winget install --id OpenJS.NodeJS.LTS -e --source winget
winget install --id Docker.DockerDesktop -e --source winget
winget install --id GitHub.cli -e --source winget
```
*Note: Restart PowerShell after installation so environment paths update.*

### 4.2 Start Docker Desktop
Launch **Docker Desktop** from the Start Menu. Verify that the Docker engine is running by checking the bottom-left icon in Docker Desktop or running:
```powershell
docker info
```

### 4.3 Configure Git Long Paths & Line Endings
```powershell
git config --global core.longpaths true
git config --global core.autocrlf false
```

### 4.4 Clone the Repository
Open a regular PowerShell window in your projects directory (e.g., `C:\Users\<user>\Projects`):
```powershell
git clone https://github.com/<your-org-or-username>/pbip.git
cd pbip
```

### 4.5 Run Environment Diagnostics
```powershell
.\dev.ps1 doctor
# Or: python scripts/dev.py doctor
```
Address any missing tools marked with `[MISSING]` or `[WARN]`.

### 4.6 Run First-Time Setup
```powershell
.\dev.ps1 setup
```
This generates `.env`, executes `uv sync --all-packages`, runs `npm ci` in `apps/web`, and sets up git pre-commit hooks.

*Note on Windows Execution Policy:* If PowerShell displays a script execution error for `dev.ps1`, run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
Alternatively, invoke the batch wrapper directly: `dev.cmd setup` or `python scripts/dev.py setup`.

---

## 5. Starting Services & Local Development

For an interactive development workflow (hot-reloading on both frontend and backend):

### Step 1: Start Infrastructure (Postgres & Redis)
*Requirement: Docker daemon running.*
- **Ubuntu:** `make infra-up`
- **Windows:** `.\dev.ps1 infra-up`

Postgres (port 5432) and Redis (port 6379) will start in detached background containers.

### Step 2: Apply Migrations & Seed Data
*Requirement: Infrastructure running.*
- **Ubuntu:**
  ```bash
  make migrate
  make seed
  ```
- **Windows:**
  ```powershell
  .\dev.ps1 migrate
  .\dev.ps1 seed
  ```
`seed` outputs the generated test passwords and seeded credentials:
- Retail customer: `alice@demo.pbip` / `bob@demo.pbip`
- Bank staff (analyst): `analyst@demo.pbip`
- Admin: `admin@demo.pbip`

### Step 3: Launch Local Dev Servers (Multiple Terminals)
Open 3 separate terminal tabs:

- **Terminal 1: FastAPI Backend**
  - Ubuntu: `make dev-api`
  - Windows: `.\dev.ps1 dev-api`
  - *URL:* http://localhost:8000 (Swagger docs at http://localhost:8000/docs)

- **Terminal 2: React Web Frontend**
  - Ubuntu: `make dev-web`
  - Windows: `.\dev.ps1 dev-web`
  - *URL:* http://localhost:5173

- **Terminal 3: Celery Background Worker**
  - Ubuntu: `make dev-worker`
  - Windows: `.\dev.ps1 dev-worker` *(Automatically uses `--pool=solo` on Windows to avoid Celery fork limitations)*

---

## 6. Running in Full Docker Stack

To run the entire bank stack inside containerized images (mirroring production staging):

### Core Banking Stack (Nginx + API + Web + Worker + DB + Redis)
```bash
# Ubuntu
make up

# Windows
.\dev.ps1 up
```
Access the application through the reverse proxy at: **http://localhost:8080**

### Extended Stack with Observability & MLflow
To also start Prometheus, Grafana, Loki, and MLflow tracking server:
```bash
# Ubuntu
make up-all

# Windows
.\dev.ps1 up-all
```
- Grafana: **http://localhost:3000** (admin / configured `GRAFANA_ADMIN_PASSWORD` from `.env`, defaults to `admin_local_password` in `.env.example`)
- Prometheus: **http://localhost:9090**
- MLflow: **http://localhost:5000**

### Performance baseline (k6)
With the API running (`make dev-api` or `make up`): `make perf` / `.\dev.ps1 perf`. Uses a native
`k6` if installed, otherwise the `grafana/k6` Docker image. Thresholds, options and the recorded
baseline are in `infra/k6/README.md`.

### MLflow only (ML work without the whole stack)
```bash
make ml-up            # or .\dev.ps1 ml-up     -> docker compose --profile ml up -d --wait mlflow
make mlflow-smoke     # or .\dev.ps1 mlflow-smoke   -> logs a dummy run and reads it back
docker compose ps mlflow   # STATUS shows "(healthy)"
```
Training code calls `pbip_ml.tracking.configure_tracking()`. It uses `MLFLOW_TRACKING_URI`
(default `http://localhost:5000`) when the server answers `/health`, and otherwise falls back to a
local SQLite store in `./mlruns/` (git-ignored) with a logged warning. Artifacts are proxied by the
server, so clients never need access to the container's volume. Add `--require-server` to the smoke
command to fail instead of falling back.

---

## 7. Running Tests, Linting & Verification

PBIP enforces strict compliance, type-safety, and testing standards.

### Fast Local Checks (No DB Required)
```bash
# Fast unit tests (Pytest unit mark)
make test-unit          # or .\dev.ps1 test-unit

# Frontend Vitest test suite
make test-web           # or .\dev.ps1 test-web

# Strict Typechecking (Mypy + TSC)
make typecheck          # or .\dev.ps1 typecheck

# Code formatting and linting (Ruff + ESLint)
make lint               # or .\dev.ps1 lint

# Architecture module import boundary check
make imports            # or .\dev.ps1 imports
```

### Full Integration & Concurrency Tests
*Requirement: PostgreSQL and Redis running via `make infra-up`.*
```bash
# Run all Python tests (unit + integration + security)
make test               # or .\dev.ps1 test
```

### Complete CI Gate Run
Run the exact check suite that runs in GitHub Actions before creating a PR:
```bash
make check              # or .\dev.ps1 check
```

### Smoke Test (Post-deployment verification)
Verify live HTTP endpoints against a running API:
```bash
make smoke              # or .\dev.ps1 smoke
```

---

## 8. URLs, Ports, Health Checks & Log Access

| Service | Host Port | Protocol | Purpose / Description | Health Endpoint |
|---|---|---|---|---|
| **API Dev** | `8000` | HTTP | FastAPI uvicorn reload server | `http://localhost:8000/api/health/live`, `http://localhost:8000/api/health/ready` |
| **Web Dev** | `5173` | HTTP | Vite development server | `http://localhost:5173/` |
| **Nginx Ingress** | `8080` | HTTP | Docker compose unified web gateway | `http://localhost:8080/api/health/live`, `http://localhost:8080/api/health/ready` |
| **PostgreSQL** | `5432` | TCP | Relational DB & Ledger storage | Docker healthcheck |
| **Redis** | `6379` | TCP | Token cache, locks, rate limits | `redis-cli ping` |
| **Grafana** | `3000` | HTTP | System dashboards & logs | `http://localhost:3000/api/health` |
| **Prometheus** | `9090` | HTTP | Metrics collection | `http://localhost:9090/-/healthy` |
| **MLflow** | `5000` | HTTP | Fraud model registry & experiment tracking | `http://localhost:5000/` |

### Viewing Logs
- Docker infrastructure logs: `docker compose logs -f postgres redis`
- All containers: `docker compose logs -f`
- Or using dev runner: `python scripts/dev.py logs`

---

## 9. Stopping Services & Workspace Cleanliness

### Stop Local Infrastructure
```bash
make infra-down         # or .\dev.ps1 infra-down
```

### Stop Docker Compose Stack
```bash
make down               # or .\dev.ps1 down
```

### Clean Temporary Caches & Build Files
Removes `__pycache__`, `.pytest_cache`, `.mypy_cache`, `dist/`, and test coverage artifacts:
```bash
make clean              # or .\dev.ps1 clean
```

---

## 10. GitHub CLI Setup & Project Automation

PBIP includes dedicated automation for repository setup and issue tracking:

### 10.1 GitHub CLI Login
Authenticate with `gh`:
```bash
gh auth login
# Select: GitHub.com -> HTTPS -> Login with a web browser
```

### 10.2 Configure Repository Settings & Milestones (`setup_github.py`)
Run the GitHub setup script in dry-run mode first:
```bash
python scripts/setup_github.py <OWNER>/<REPO> --collaborators <COLLABORATOR_USERNAME> --dry-run
```
When ready to apply configuration (labels, 7 sprint milestones, merge options):
```bash
python scripts/setup_github.py <OWNER>/<REPO> --collaborators <COLLABORATOR_USERNAME>
```

### 10.3 Backlog Validation & Issue Import (`backlog.py`)
Validate the 93-task backlog DAG:
```bash
python scripts/backlog.py --validate
```
Test issue creation in dry-run mode (e.g., for Sprint 1):
```bash
python scripts/backlog.py <OWNER>/<REPO> --sprint 1 --dry-run
```
Import issues to GitHub with milestone assignment and project tracking:
```bash
# Import a specific sprint (e.g., Sprint 1)
python scripts/backlog.py <OWNER>/<REPO> --sprint 1 --project-number 1

# Or import all sprints
python scripts/backlog.py <OWNER>/<REPO> --project-number 1
```

---

## 11. Team Contributor Onboarding

When a new team contributor sets up their machine:

1. Contributor clones the repository:
   ```bash
   git clone https://github.com/<OWNER>/<REPO>.git
   cd pbip
   ```
2. Contributor runs diagnostics:
   - Ubuntu: `make doctor`
   - Windows: `.\dev.ps1 doctor`
3. Contributor sets up dependencies and `.env`:
   - Ubuntu: `make setup`
   - Windows: `.\dev.ps1 setup`
4. Contributor boots local databases, runs migrations and seeds data:
   ```bash
   make infra-up && make migrate && make seed
   # Windows: .\dev.ps1 infra-up; .\dev.ps1 migrate; .\dev.ps1 seed
   ```
5. Contributor validates test suite:
   ```bash
   make test-unit && make test-web
   ```
6. **Collaboration Workflow:**
   - Pick an issue from the GitHub Project board (column: `Ready`).
   - Move issue to `In Progress` and assign to self.
   - Branch: `git switch -c feat/<issue-id>-<short-description>`.
   - Write code + tests + verify with `make check`.
   - Open Pull Request referencing the task (e.g. `Resolves #12`).
   - Peer review PR -> CI Gate passes -> Squash-merge to `main`.

---

## 12. Common Troubleshooting & FAQ

### Q: `docker: permission denied` on Linux
**Fix:** Add your user to the docker group:
```bash
sudo usermod -aG docker $USER
newgrp docker
```

### Q: `Celery: ValueError: not enough values to unpack` or spawn errors on Windows
**Fix:** Windows does not support Celery `prefork` process pooling. The PBIP dev runner automatically handles this by appending `--pool=solo` when running `.\dev.ps1 dev-worker` on Windows. If invoking Celery manually:
```powershell
uv run celery -A pbip_api.worker.celery_app worker --loglevel=INFO --pool=solo
```

### Q: `Execution of scripts is disabled on this system` in Windows PowerShell
**Fix:** Set your execution policy to allow local signed scripts:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
Or run the batch entrypoint: `dev.cmd <command>` or `python scripts/dev.py <command>`.

### Q: `tests skip with: run make infra-up`
**Fix:** Integration tests require live Postgres and Redis instances. Start them with:
```bash
make infra-up
# or .\dev.ps1 infra-up
```

### Q: Port 5432 or 6379 already in use
**Fix:** Another PostgreSQL or Redis server is running locally on your machine.
- Linux: `sudo systemctl stop postgresql redis-server`
- Windows: Open Services (`services.msc`) and stop the local `postgresql-x64` or `redis` service before running `make infra-up`.

### Q: Frontend package installations fail
**Fix:** Ensure you have Node.js 20+ installed. In `apps/web`, run:
```bash
npm ci
```
Do not hand-edit `package-lock.json`.
