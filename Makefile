# PBIP Developer Makefile
# Thin wrapper that delegates all commands to cross-platform scripts/dev.py.
# On Windows, use dev.ps1 or dev.cmd instead of make.
.DEFAULT_GOAL := help
.PHONY: help doctor setup env infra-up infra-down up up-all down logs migrate seed \
	datagen fraud-model notebooks ml-up mlflow-smoke dev-api dev-worker dev-web lint fmt typecheck imports security openapi \
	test test-unit test-int test-web test-e2e smoke perf fuzz check clean

PYTHON ?= python3

help: ## Show this help
	@$(PYTHON) scripts/dev.py help

doctor: ## Run diagnostic checks for platform, tools, and ports
	@$(PYTHON) scripts/dev.py doctor

setup: ## One-time setup: .env, Python + Node deps, git hooks
	@$(PYTHON) scripts/dev.py setup

env: ## Create .env with generated local secrets
	@$(PYTHON) scripts/dev.py env

infra-up: ## Start only PostgreSQL + Redis in Docker
	@$(PYTHON) scripts/dev.py infra-up

infra-down: ## Stop PostgreSQL + Redis
	@$(PYTHON) scripts/dev.py infra-down

up: ## Build and start the full PBIP stack (web on :8080)
	@$(PYTHON) scripts/dev.py up

up-all: ## Full stack + monitoring (Prometheus, Grafana) + MLflow
	@$(PYTHON) scripts/dev.py up-all

down: ## Stop everything
	@$(PYTHON) scripts/dev.py down

logs: ## Tail API logs
	@$(PYTHON) scripts/dev.py logs

migrate: ## Apply database migrations (local DB)
	@$(PYTHON) scripts/dev.py migrate

seed: ## Seed demo users, accounts and MFA-enrolled staff (local only)
	@$(PYTHON) scripts/dev.py seed

datagen: ## Generate + COPY-load the synthetic ML dataset (seed 42, 1,000 customers)
	@$(PYTHON) scripts/dev.py datagen

fraud-model: ## Train the fraud models into models/fraud (needed for FRAUD_SCORER=hybrid)
	@$(PYTHON) scripts/dev.py fraud-model

notebooks: ## Execute the EDA notebooks head-to-tail and save outputs
	@$(PYTHON) scripts/dev.py notebooks

ml-up: ## Start only the MLflow tracking server on :5000
	@$(PYTHON) scripts/dev.py ml-up

mlflow-smoke: ## Log + read back a dummy MLflow run (server, else local fallback)
	@$(PYTHON) scripts/dev.py mlflow-smoke

dev-api: ## Run the API with auto-reload on :8000
	@$(PYTHON) scripts/dev.py dev-api

dev-worker: ## Run the Celery worker + beat locally (solo pool on Windows)
	@$(PYTHON) scripts/dev.py dev-worker

dev-web: ## Run the web app (Vite) on :5173
	@$(PYTHON) scripts/dev.py dev-web

lint: ## Lint + format check (Python and web)
	@$(PYTHON) scripts/dev.py lint

fmt: ## Auto-format everything (Python and web)
	@$(PYTHON) scripts/dev.py fmt

typecheck: ## mypy (strict) + tsc
	@$(PYTHON) scripts/dev.py typecheck

imports: ## Enforce module boundaries (import-linter)
	@$(PYTHON) scripts/dev.py imports

security: ## Static analysis + dependency audit (Bandit, pip-audit, npm audit)
	@$(PYTHON) scripts/dev.py security

openapi: ## Regenerate OpenAPI contract and typed web client
	@$(PYTHON) scripts/dev.py openapi

test: ## Backend + ML tests with coverage (needs make infra-up)
	@$(PYTHON) scripts/dev.py test

test-unit: ## Fast tests only (no database needed)
	@$(PYTHON) scripts/dev.py test-unit

test-int: ## Integration tests (PostgreSQL)
	@$(PYTHON) scripts/dev.py test-int

test-web: ## Frontend tests (Vitest)
	@$(PYTHON) scripts/dev.py test-web

test-e2e: ## Playwright end-to-end (needs make up)
	@$(PYTHON) scripts/dev.py test-e2e

smoke: ## Post-deploy smoke test
	@$(PYTHON) scripts/dev.py smoke

perf: ## k6 baseline on health endpoints (50 VUs, p95 < 50 ms); needs a running API
	@$(PYTHON) scripts/dev.py perf

fuzz: ## Property-based API fuzzing with Schemathesis against OpenAPI spec
	@$(PYTHON) scripts/dev.py fuzz

check: ## EVERYTHING CI runs. Run before opening a PR.
	@$(PYTHON) scripts/dev.py check

clean: ## Remove caches and build output
	@$(PYTHON) scripts/dev.py clean
