# PBIP developer commands. On Windows use WSL2 (Ubuntu) - see README.
.DEFAULT_GOAL := help
.PHONY: help setup env infra-up infra-down up up-all down logs migrate seed dev-api dev-worker dev-web \
	test test-unit test-int test-web test-e2e lint fmt typecheck imports security openapi check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: env ## One-time setup: .env, Python + Node deps, git hooks
	uv sync --all-packages
	cd apps/web && npm ci
	uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push

env: ## Create .env with generated local secrets
	uv run python scripts/init_env.py

infra-up: ## Start only PostgreSQL + Redis (for running API/tests on your machine)
	docker compose up -d --wait db redis

infra-down: ## Stop PostgreSQL + Redis
	docker compose stop db redis

up: ## Build and start the full stack (web on http://localhost:8080)
	docker compose up -d --build

up-all: ## Full stack + monitoring + MLflow
	docker compose --profile monitoring --profile ml up -d --build

down: ## Stop everything
	docker compose --profile monitoring --profile ml down

logs: ## Tail API logs
	docker compose logs -f api

migrate: ## Apply database migrations (local DB)
	uv run alembic -c apps/api/alembic.ini upgrade head

seed: ## Seed demo users, accounts and MFA-enrolled staff (local only)
	uv run python scripts/seed_dev.py

dev-api: ## Run the API with auto-reload on :8000
	uv run uvicorn pbip_api.main:app --reload --port 8000

dev-worker: ## Run the Celery worker + beat locally
	uv run celery -A pbip_api.worker.celery_app worker --beat --loglevel=INFO

dev-web: ## Run the web app (Vite) on :5173
	cd apps/web && npm run dev

test: ## Backend + ML tests with coverage (needs `make infra-up`)
	uv run pytest --cov

test-unit: ## Fast tests only (no database needed)
	uv run pytest -m "unit or security and not integration" -q

test-int: ## Integration tests (PostgreSQL)
	uv run pytest -m integration -q

test-web: ## Frontend tests
	cd apps/web && npm test

test-e2e: ## Playwright end-to-end (needs `make up`)
	cd apps/web && npx playwright install --with-deps chromium && npm run test:e2e

lint: ## Lint + format check (Python and web)
	uv run ruff check . && uv run ruff format --check .
	cd apps/web && npm run lint && npm run format:check

fmt: ## Auto-format everything
	uv run ruff check --fix . && uv run ruff format .
	cd apps/web && npm run format

typecheck: ## mypy (strict) + tsc
	uv run mypy apps/api/src ml/src data-gen/src
	cd apps/web && npm run typecheck

imports: ## Enforce module boundaries (import-linter)
	uv run lint-imports

security: ## Static analysis + dependency audit
	uv run bandit -q -r apps/api/src ml/src data-gen/src -ll
	uv export --frozen --all-packages --no-hashes --no-emit-workspace -o /tmp/pbip-req.txt >/dev/null && uv run pip-audit -r /tmp/pbip-req.txt --no-deps
	cd apps/web && npm audit --audit-level=high

openapi: ## Regenerate the OpenAPI contract and the typed web client
	uv run python scripts/export_openapi.py
	cd apps/web && npm run gen:api

check: lint typecheck imports security test test-web ## EVERYTHING CI runs. Run before opening a PR.
	uv run python scripts/export_openapi.py --check

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml apps/web/dist apps/web/coverage
