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
- **API (`apps/api`):** FastAPI modular monolith (Python 3.12, SQLAlchemy 2 **synchronous** - ADR 0005, Alembic, Celery, Redis, argon2id).
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
| **Synthetic ML Dataset** | `make datagen` | `.\dev.ps1 datagen` | `python scripts/dev.py datagen` |
| **Run EDA Notebooks** | `make notebooks` | `.\dev.ps1 notebooks` | `python scripts/dev.py notebooks` |
| **MLflow Server Only** | `make ml-up` | `.\dev.ps1 ml-up` | `python scripts/dev.py ml-up` |
| **k6 Perf Baseline** | `make perf` | `.\dev.ps1 perf` | `python scripts/dev.py perf` |

### Prerequisite versions

| Tool | Minimum | Notes |
|---|---|---|
| Python | **3.12** | `uv` downloads it automatically if missing (`uv python install 3.12`) |
| uv | 0.4 | Python package/venv manager; installs to `~/.local/bin` |
| Node.js / npm | 20 (22 LTS recommended) | Build tooling only - nothing Node runs in production |
| Docker Engine + Compose v2 | Docker 24, `docker compose` plugin | Docker Desktop on Windows/macOS |
| git | 2.40 | |
| GitHub CLI (`gh`) | optional | Only for `scripts/setup_github.py` and backlog import |
| k6 | optional | `make perf` falls back to the `grafana/k6` Docker image |

`make doctor` / `.\dev.ps1 doctor` checks all of these and the ports below.
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
export PATH="$HOME/.local/bin:$PATH"  # the installer's location (or restart your terminal)
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
winget install --id astral-sh.uv -e --source winget
winget install --id OpenJS.NodeJS.LTS -e --source winget
winget install --id Docker.DockerDesktop -e --source winget
winget install --id GitHub.cli -e --source winget
```
*Note: Restart PowerShell after installation so environment paths update. `-e` matches the id
case-sensitively: it is `astral-sh.uv` (`Astral-sh.uv` fails with "No package found").*

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

### 4.7 Windows verification status (honest)
The Windows-specific paths (TASK-001 runner, TASK-012 k6 runner, TASK-013 guide) **passed on a real Windows 10 machine**
on 2026-10-27 (third run, below) after two runs that found and fixed defects. Re-run the checklist after any change to
the runner, the k6 baseline or this guide. One native Windows run of steps 0-8 is the evidence for all three:

| Task (issue) | Steps that prove its Windows criteria |
|---|---|
| TASK-001 (GitHub issue #12) runner | 3, 5, 6, 8 (exit codes in PowerShell and `cmd.exe`, solo pool + separate beat, clean) |
| TASK-012 (GitHub issue #23) k6 baseline | 7 ("Cross-platform execution script works on Windows and Linux") |
| TASK-013 (GitHub issue #24) guide | 0-4 from a fresh clone, plus the whole run following only this guide |

**Verified on Linux (Ubuntu, kernel 7.0, Python 3.12, 2026-10-25):**
- 62 unit tests for the runner, doctor and setup scripts pass, including the Windows code paths exercised by
  patching the platform flag (solo pool and separate beat, `dev.cmd` exit-code regression).
- `python scripts/dev.py <unknown>` exits 1 and `help` exits 0; `doctor` reports 12 OK, 0 WARN, 0 MISSING.

**First native Windows run (Windows 10 Pro 22H2, build 19045, PowerShell 5.1, 2026-10-26, commit `86f94a3`):**
steps 0, 1, 3 and 8 passed; 2, 4, 5, 6 and 7 found defects (evidence on GitHub issues #12, #23, #24). Fixed here: `setup` failing
on a fresh clone (`init_env.py` imported `cryptography` before dependencies were installed), `dev.cmd` exiting 0
for a failing command when run as `cmd /c` or from PowerShell, a unit test that was not portable to Windows, the
`winget` id for uv, and checklist commands in steps 5 and 7 that could never show a failure. The tasks stay
**In Review** until those fixes are merged and steps 2-8 pass on Windows again.

**Second native Windows run (same machine, 2026-10-26, merged `main` `04df85f` with GitHub PRs #132 and #133, fresh clone):**
steps 0-6 and 8 pass. Setup works from a clean clone; `test-unit` 272 passed; `dev.ps1` and `dev.cmd` return 1/0 as the
process exit code, including through `2>&1 | Tee-Object` and `*> file`; one console Ctrl+C on a ready worker stops
worker and beat in about 4 s with no process left. **Step 7 still fails its latency thresholds** on this 2-core laptop
(`ready` p95 81.5 / 100.69 ms, overall 62.73 / 80.94 ms; errors 0.00%, checks 100%, both commands exit 99): GitHub issue #134.
`.\dev.ps1 check`: 419 passed, coverage 95.78%, all lint/type/security gates green; one TASK-009 COPY-budget
test took 15.9-17.2 s against 15 s (GitHub issue #135). TASK-001, TASK-012 and TASK-013 therefore stay **In Review**.

**Third native Windows run (same machine, 2026-10-27, `main` `9386b22` with GitHub PRs #137 and #138, fresh clone): steps 0-8
pass.** Setup exit 0 from a clean clone; migrate exit 0; `test-unit` 283 passed; both runners return 1/0 (also through
`2>&1 | Tee-Object` and `cmd /c`); one console Ctrl+C after a task was received: warm shutdown, task finished, every
process gone in 1.79 s. Step 7 after GitHub PR #137 (`/ready` no longer takes four thread-pool hops per probe): `dev.cmd perf`
p95 overall 15.44 / 16.00 ms, live 14.22 / 14.86, ready 16.53 / 16.72; `.\dev.ps1 perf` 38.53 / 35.58 / 41.69 ms; errors
0.00%, checks 100%, exit 0. One further `.\dev.ps1 perf` run failed (182 ms) while other apps saturated the CPU; `/live`,
which does no I/O, failed by the same margin, so that run measured the machine, not the endpoint. Thresholds unchanged.
`.\dev.ps1 check`: 430 passed, coverage 95.79%, all lint/type/security gates green; the TASK-009 COPY-budget test is
still over 15 s on this 2-core laptop (GitHub issue #135, not part of these tasks' criteria). TASK-001, TASK-012 and TASK-013: **Done**.

**Copy-paste checklist (Windows 10/11, PowerShell; Docker Desktop running).** Paste the output of steps 0-8 into
GitHub issues #12 (TASK-001), #23 (TASK-012) and #24 (TASK-013).

**Second checkout on the same machine (GitHub issue #371).** `docker-compose.yml` names the project `pbip`, so every
checkout shares the `pbip_pgdata` volume, which keeps the `POSTGRES_PASSWORD` of the checkout that created it. A fresh
clone's newly generated `.env` password is then rejected and step 4's `migrate` fails with `password authentication
failed for user "pbip"`. Give the second clone its own project, and so its own volumes, by adding one line to **its**
`.env` after step 2: `COMPOSE_PROJECT_NAME=pbip-wincheck`. Compose reads `COMPOSE_PROJECT_NAME` from the `.env` next to
`docker-compose.yml` and it takes precedence over the file's `name:` (only `-p` ranks higher); check it with
`docker compose config | Select-String "name: pbip"`, which must list `pbip-wincheck_pgdata` (it only prints the
resolved configuration and starts nothing). The ports are fixed (5432, 6379, ...), so stop the first checkout's stack before step 4
(`.\dev.ps1 infra-down` there; this keeps its data). Do not `docker compose down -v` to "make room": that deletes the
first checkout's database. Leave `name: pbip` in the file as it is: changing it would orphan existing volumes.

```powershell
# 0. Environment (record it)
[System.Environment]::OSVersion.VersionString; python --version; uv --version; node --version; git --version
docker version --format '{{.Server.Version}}'

# 1. Fresh clone + diagnostics (TASK-002/013): expect [OK]/[WARN] lines and exit code 0
git clone https://github.com/<your-org-or-username>/pbip.git pbip-wincheck; cd pbip-wincheck
#    If PowerShell refuses to run dev.ps1, apply the execution-policy note in section 4.6 (RemoteSigned, CurrentUser).
.\dev.ps1 doctor; "exit=$LASTEXITCODE"

# 2. Setup from the guide (TASK-013): expect .env created, Python + npm deps installed, exit 0
.\dev.ps1 setup; "exit=$LASTEXITCODE"

# 3. dev.ps1 propagates exit codes (TASK-001): expect exit=1, then exit=0
.\dev.ps1 not-a-command; "exit=$LASTEXITCODE"
.\dev.ps1 help; "exit=$LASTEXITCODE"

# 4. Infrastructure, migrations, fast tests (TASK-013): expect exit=0 for each
.\dev.ps1 infra-up; "exit=$LASTEXITCODE"
.\dev.ps1 migrate; "exit=$LASTEXITCODE"
.\dev.ps1 test-unit; "exit=$LASTEXITCODE"
```

```bat
:: 5a. dev.cmd sets ERRORLEVEL (TASK-001), in cmd.exe from the repo root, one line at a time:
::     expect exit=1, then exit=0. Do not join them with "&": %ERRORLEVEL% is expanded when the
::     line is read, before dev.cmd runs, so a one-line "dev.cmd ... & echo %ERRORLEVEL%" always
::     prints the previous value.
dev.cmd not-a-command
echo exit=%ERRORLEVEL%
dev.cmd help
echo exit=%ERRORLEVEL%
```

```powershell
# 5b. cmd.exe's own exit code (what CI and scripts see), from PowerShell: expect exit=1, then exit=0
cmd /c dev.cmd not-a-command; "exit=$LASTEXITCODE"
cmd /c dev.cmd help; "exit=$LASTEXITCODE"
```

```powershell
# 6. Celery on Windows (TASK-001): expect worker banner with "pool: solo" (or concurrency 1 (solo)) AND a separate
#    "beat: Starting..." line; no "-B option does not work on Windows" error. Then press Ctrl+C and confirm no
#    celery process remains:
.\dev.ps1 dev-worker
Get-Process | Where-Object { $_.ProcessName -match 'celery|python' } | Select-Object Id, ProcessName

# 7. k6 baseline (TASK-012): in terminal A run `.\dev.ps1 dev-api`; in terminal B run the two commands below.
#    Record which k6 ran: native `k6` if `Get-Command k6` finds it (API at localhost:8000), otherwise the Docker
#    image (API reached at host.docker.internal:8000). Expect a ramp to 50 VUs, then every threshold ticked:
#    http_req_failed rate<0.001, http_req_duration p(95)<50 (overall, {endpoint:live}, {endpoint:ready}),
#    checks rate>0.999, and exit=0. If k6 runs but a latency threshold fails, paste the summary anyway: the
#    runner works but the threshold criterion is not met, which is a finding, not a pass.
Get-Command k6 -ErrorAction SilentlyContinue
.\dev.ps1 perf; "exit=$LASTEXITCODE"
cmd /c dev.cmd perf; "exit=$LASTEXITCODE"

# 8. Clean (TASK-001): expect caches removed and exit=0
.\dev.ps1 clean; "exit=$LASTEXITCODE"
```

Pass criteria: every expected result above holds. Any deviation is a defect: open a `type:bug` issue linked to
TASK-001/012/013 with the output. None of the three issues moves to Done until this evidence is posted on it.

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
`seed` prints the seeded accounts, never a password or an MFA secret. Every new user gets the password in
`PBIP_DEMO_PASSWORD` if you set it (e.g. `PBIP_DEMO_PASSWORD=<one you choose> make seed`); without it, and only
with `APP_ENV=local`, the `DEMO_PASSWORD` constant in `scripts/seed_dev.py`. Any other `APP_ENV` must set the
variable (the CI E2E job generates one per run, see `docs/testing/e2e.md`). Existing users are skipped, so the
password of an already-seeded database does not change.
- Retail customers: `alice@demo.pbip` (authenticator enrolled, for the step-up dialog; code: `uv run python scripts/totp.py alice@demo.pbip`), `bob@demo.pbip` and `carol@demo.pbip` (no authenticator: the fraud demo lab signs in as Carol, F6 sender Bob)
- Bank staff (analyst): `analyst@demo.pbip`
- Admin: `admin@demo.pbip`
- Relationship manager: `rm@demo.pbip` (RM insights page, TASK-066)

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
  - Windows: `.\dev.ps1 dev-worker` *(Celery has no prefork pool and rejects `--beat` on Windows, so the
    worker runs with `--pool=solo` and the beat scheduler is started as a separate child process; both stop
    together when you press Ctrl+C)*

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
Access the application through the reverse proxy at: **https://localhost:8443** (HTTP on 8080 redirects; the certificate comes from the local dev CA created by `scripts/dev_certs.py`, so the browser warns until you accept it)

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

### Tracing: optional OpenTelemetry Collector (`otel` profile, ADR-0049)
Tracing is off by default. To see traces from the API, worker and beat:

1. In `.env`: `OTEL_ENABLED=true`, `OTEL_EXPORTER=otlp` (optionally `OTEL_SAMPLE_RATE=0.1`, and
   `OTEL_COLLECTOR_VERBOSITY=detailed` to print every span with its attributes).
2. `docker compose --profile otel up -d otel-collector`, then recreate the services that read the
   variables: `docker compose up -d api worker beat` (or restart `make dev-api`, which sends to
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`, the collector's loopback port).
3. `docker compose logs -f otel-collector` shows the spans: one SERVER span per request with its SQL
   and Redis CLIENT children, and `apply_async/<task>` (service `pbip-api-beat`) followed by
   `run/<task>` (service `pbip-api-worker`) in the same trace for every scheduled task.

The collector keeps nothing and sends nothing on; it only logs (`infra/otel/collector.yml`). If it is
stopped or unreachable, the API drops the spans after `OTEL_EXPORT_TIMEOUT_SECONDS` (default 2) on a
background thread and logs `Failed to export spans batch`; requests are not slowed down. Turn it off
again with `OTEL_ENABLED=false` and `docker compose --profile otel stop otel-collector`.

### Performance baseline (k6)
With the API running (`make dev-api` or `make up`): `make perf` / `.\dev.ps1 perf`. Uses a native
`k6` if installed, otherwise the `grafana/k6` Docker image. Thresholds, options and the recorded
baseline are in `infra/k6/README.md`.

### AI assistant: Ollama on the host
The assistant calls a local LLM through Ollama; it never needs a cloud key. If Ollama is not
running, the assistant answers from a deterministic keyword router instead, so a demo never fails.
Clear questions ("what's my balance", "groceries last month", "5 lakh car loan over 5 years") never
call the model: they go straight to their tool and answer in well under a second. Only unclear
questions are sent to Ollama.

```bash
curl -fsSL https://ollama.com/install.sh | sh      # Windows/macOS: install from ollama.com
ollama pull qwen2.5:1.5b-instruct                  # ~1 GB; the default (qwen2.5:3b-instruct also works)
curl -s http://localhost:11434/api/tags            # the model is listed
```

* `make dev-api` reads `OLLAMA_BASE_URL=http://localhost:11434` and `OLLAMA_MODEL` from `.env`.
* The compose `api` container reaches the host through `host.docker.internal`
  (`extra_hosts: host-gateway` in `docker-compose.yml`; Docker Desktop provides it already) and uses
  `OLLAMA_CONTAINER_URL`, default `http://host.docker.internal:11434`.
* On Linux, Ollama listens on 127.0.0.1 by default. For the container to reach it, run it on the
  Docker bridge too: `sudo systemctl edit ollama` → `Environment="OLLAMA_HOST=0.0.0.0:11434"`, then
  `sudo systemctl restart ollama`; keep port 11434 closed to other machines with your firewall.
* `OLLAMA_TIMEOUT_SECONDS` (default 4): past this the keyword router answers an unclear question
  instead. On the reference laptop (Celeron N4500, 2 cores, no AVX) one routing call takes 12-30 s
  with the 1.5B model and 41-50 s with the 3B model once loaded, so unclear questions show
  `engine: rules` there unless you raise the timeout or have faster hardware.
  `ASSISTANT_LLM_ENABLED=false` skips the model entirely.
* `OLLAMA_KEEP_ALIVE` (default `24h`): how long Ollama keeps the model in memory after a question.
  The first call after a load took 3-5 minutes on the reference laptop. Pre-warm before a demo:
  `ollama run qwen2.5:1.5b-instruct ""`.
* Try it: `POST /api/v1/assistant/chat` with `{"message": "what's my balance?"}` in Swagger (`/docs`).

### AI assistant: optional Groq router (demo only, ADR-0037)
On slow hardware the local model rarely answers within its timeout. For a demo the owner can route
**unclear** questions through Groq's hosted API instead (clear questions and refusals still never call
any model). Off by default; it sends the redacted question (no ids, account numbers, emails, phone
numbers or your name) to a US-hosted provider, so use it only where the owner approved that.

* In `.env`: `LLM_PROVIDER=groq`, `GROQ_API_KEY=<from console.groq.com/keys>` (never commit or paste it
  anywhere), `GROQ_MODEL` and `GROQ_REASONING_EFFORT` as chosen in ADR-0037. Then restart the API
  (`docker compose up -d api` or `make dev-api`).
* Order: Groq, then Ollama, then the keyword rules; a timeout (5 s), 429 or bad answer from Groq just
  moves to the next step. Each customer gets `RATE_LIMIT_GROQ_PER_MINUTE` (default 5) Groq calls a minute.
* Check: an unclear question ("am I doing ok money-wise?") answers with `engine: llm`; the audit log has an
  `assistant.llm_call` row with `provider=groq`.
* Key handling and rotation: `docs/operations/runbook.md` section 3.3.

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
| **Nginx Ingress** | `8443` (`8080` redirects) | HTTPS | Docker compose unified web gateway | `curl --cacert infra/certs/ca.crt https://localhost:8443/api/health/ready` |
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

### Q: `Celery: ValueError: not enough values to unpack`, spawn errors, or `-B option does not work on Windows`
**Fix:** Windows has no Celery `prefork` pool and Celery refuses `--beat`/`-B` there. `.\dev.ps1 dev-worker`
runs the worker with `--pool=solo` and starts beat as a separate process automatically. If invoking Celery
manually, use two terminals:
```powershell
uv run celery -A pbip_api.worker.celery_app worker --loglevel=INFO --pool=solo
uv run celery -A pbip_api.worker.celery_app beat --loglevel=INFO
```

### Q: `Execution of scripts is disabled on this system` in Windows PowerShell
**Fix:** Set your execution policy to allow local signed scripts:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
Or run the batch entrypoint: `dev.cmd <command>` or `python scripts/dev.py <command>`.

### Q: Passing flags or capturing output with `dev.ps1`
`.\dev.ps1` forwards every argument unchanged, including flags such as `-p`, `-v`, `-d` or `-k`
(`.\dev.ps1 test --no-cov -k ledger -v`). PowerShell itself consumes a bare `--`; quote it (`'--'`) to pass it on.
Capturing output works and keeps the real exit code: `.\dev.ps1 check 2>&1 | Tee-Object check.log` or
`.\dev.ps1 check *> check.log`. Windows PowerShell 5.1 shows redirected stderr lines as red `NativeCommandError`
text; that is display only, and the command keeps running.

### Q: `tests skip with: run make infra-up`
**Fix:** Integration tests require live Postgres and Redis instances. Start them with:
```bash
make infra-up
# or .\dev.ps1 infra-up
```

### Q: On Windows the worker hangs on its first task, `/api/health/ready` times out, or `perf` misses p95 < 50 ms
**Cause:** an older `.env` points `DATABASE_URL`, `TEST_DATABASE_URL` or `REDIS_URL` at `localhost`. Windows
tries `::1` first, but compose publishes Postgres and Redis on `127.0.0.1` only, so every new connection stalls
(measured: 15 s per connect with a 15 s timeout, indefinitely without one).
**Fix:** `.env` is never overwritten, so edit it yourself: replace `localhost` with `127.0.0.1` in those three
URLs (current `.env.example` already uses it), then restart `dev-api` / `dev-worker`.

### Q: Transfers are scored "rules-only" with `model_error=TIMEOUT` on a slow demo machine
**Cause:** with `FRAUD_SCORER=hybrid` the model step waits at most `FRAUD_MODEL_BUDGET_MS` (default 50 ms).
Past it the API serves the rules' decision, marked `+rules-only` with a `MODEL_UNAVAILABLE` reason (fail-safe,
never ALLOW-all). On a slow or busy laptop the model step alone can take longer than 50 ms, so some transfers
show no model reasons.
**Fix:** measure the machine first (no database needed; it reads `models/fraud`, never writes it):
```bash
uv run python scripts/benchmark_fraud_latency.py --model-dir models/fraud --calls 500 \
    --budget-ms 50 --budget-ms 100
```
It prints p50/p95/p99 of the model step, the fallback rate at each budget and the load average. If the rate at
50 ms is too high for the demo, set `FRAUD_MODEL_BUDGET_MS` in `.env` (allowed 5-100; the API refuses to start
outside that range, because the scoring contract is p95 < 100 ms). The compose stack passes it to `api`, `worker`
and `beat`: run `docker compose up -d api worker beat` to apply it (natively, restart `dev-api` and `dev-worker`).
Close other heavy programs first: the rate depends mostly on the load at the time.

Measured on 3 Nov 2026, 2-CPU team laptop, `models/fraud` (`hybrid-lgbm-iforest-1`), 500 calls per budget, one
transfer at a time:

| Load average (1 min) | Model step p50 / p95 / p99 | Fallback at 50 ms | Fallback at 100 ms |
|---|---|---|---|
| 1.2-1.4 (idle) | 14.5 / 19.4 / 38.5 ms | 0.2 % (1 of 500) | 0 % |
| 3.6-4.1 (other jobs running) | 16.7 / 47.7 / 78.5 ms | 1.0 % (5 of 500) | 1.0 % (5 of 500) |

Back to back (no pause between calls) at load 5.5-5.7 the 50 ms budget fell back on 8.2 % of calls and 100 ms on
0.8 %. On the live stack at load 5-6 (database look-ups and other containers competing too) a 50 ms budget timed
out on all 3 demo transfers tried and 90 ms on 1 of 4. For a demo on this laptop use `FRAUD_MODEL_BUDGET_MS=100`
and keep the load low.

### Q: A port is already in use (`bind: address already in use`)
PBIP uses 5432 (PostgreSQL), 6379 (Redis), 8000 (API), 5173 (Vite), 8080/8443 (web/nginx), 3000 (Grafana),
9090 (Prometheus) and 5000 (MLflow). `make doctor` reports which ones are taken. Find the owner:
```bash
# Linux / macOS
sudo ss -ltnp 'sport = :8000'          # or: sudo lsof -i :8000
```
```powershell
# Windows PowerShell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess }
```
**Fix:** stop the other process, or stop a stale PBIP stack with `make down` / `.\dev.ps1 down`.
Common culprits: a native PostgreSQL/Redis service (Linux: `sudo systemctl stop postgresql redis-server`;
Windows: stop `postgresql-x64-*` / `Redis` in `services.msc`), a second `dev-api` still running (8000), or
macOS AirPlay Receiver on 5000 (System Settings > General > AirDrop & Handoff).

### Q: Frontend package installations fail
**Fix:** Ensure you have Node.js 20+ installed. In `apps/web`, run:
```bash
npm ci
```
Do not hand-edit `package-lock.json`.
