# Contributing to PBIP

PBIP is built to the standards of a regulated bank: **correctness, security and auditability outrank speed.**
This guide is the practical workflow for every change, including changes written with an AI coding assistant.
`AGENTS.md` holds the binding engineering rules; where this guide summarises them, `AGENTS.md` wins.

Core rule: **nothing reaches `main` without a pull request, a green `CI Gate` and review.**

## 1. Getting started

1. Clone and start from the latest `main`:
   ```bash
   git clone https://github.com/rachitst/pbip.git
   cd pbip
   git switch main && git pull
   ```
2. Read, in this order: [`AGENTS.md`](AGENTS.md) (rules), [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md) (setup on
   Ubuntu, macOS or native Windows), [`docs/PROJECT_BLUEPRINT.md`](docs/PROJECT_BLUEPRINT.md) (architecture, source of truth).
3. Find your work: [`docs/TEAM_EXECUTION_PLAN.md`](docs/TEAM_EXECUTION_PLAN.md) (owners, target deadlines, dependencies,
   handoffs), [`docs/backlog/backlog.toml`](docs/backlog/backlog.toml) (acceptance criteria and definitions of done) and the
   [GitHub Project board](https://github.com/users/rachitst/projects/1) (status). Each task has a GitHub issue with the
   same acceptance criteria.
4. Set up the environment exactly as the setup guide says:

   | Step | Linux / macOS | Windows PowerShell |
   |---|---|---|
   | Check prerequisites | `make doctor` | `.\dev.ps1 doctor` |
   | `.env`, dependencies, git hooks | `make setup` | `.\dev.ps1 setup` |
   | PostgreSQL + Redis | `make infra-up` | `.\dev.ps1 infra-up` |
   | Database schema | `make migrate` | `.\dev.ps1 migrate` |
   | Demo data (local only) | `make seed` | `.\dev.ps1 seed` |

   `make setup` generates `.env` with fresh local secrets and installs the pre-commit, commit-msg and pre-push hooks.
   `.env` is git-ignored: **never commit it**, never copy real credentials into it, never share it.

> **Obsolete secrets file.** Older versions of the seed script wrote plaintext MFA secrets to `.dev-secrets.txt`. If you
> ever ran that old version, delete the file (`rm .dev-secrets.txt`, or `Remove-Item .dev-secrets.txt` on Windows).
> Staff MFA codes now come from `uv run python scripts/totp.py <staff email>`, which decrypts the secret from your local
> database; the demo password is the `DEMO_PASSWORD` constant in `scripts/seed_dev.py`.

## 2. Task and branch workflow

1. **Work only on tasks assigned to you.** Before starting, check the task's dependencies in the execution plan and on
   the issue: every upstream task must be merged and its owner must have posted the interface/contract and proving tests.
   If a dependency is not ready, record the blocker on your issue instead of building around it.
2. Move the issue to **In Progress** on the board.
3. Create one branch per task from the latest `main`:
   ```bash
   git switch main && git pull
   git switch -c feat/task-0NN-short-name
   ```
   | Prefix | Use for |
   |---|---|
   | `feat/` | new behaviour from a backlog task |
   | `fix/` | bug fixes (always with a regression test) |
   | `docs/` | documentation only |
   | `test/` | tests only |
   | `ci/` | workflows, rulesets, build tooling |
4. **Keep the change to one task.** No drive-by refactors, reformatting of untouched code, unrelated cleanup or
   dependency bumps. Aim for PRs under ~400 changed lines. Found something else? Open an issue for it.
5. Commit in small steps with **Conventional Commits**; the commit-msg hook rejects anything else (see section 4 for the
   types). Never use `--no-verify`, and never disable a hook.
6. Stay current with `main` (at least daily). `main` only accepts branches that are up to date, so update before review:
   ```bash
   git fetch origin
   git rebase origin/main            # resolve conflicts, re-run tests
   git push --force-with-lease       # only on your own task branch
   ```
   Never force-push a branch someone else is also working on (merge `origin/main` into it instead), and never push to `main`.

## 3. Rules for AI coding assistants

The developer, not the assistant, is responsible for every line submitted.

* **Give the assistant the rules and the task.** Start each session with `AGENTS.md` and the task's acceptance criteria,
  definition of done and dependencies (copy them from the issue or `backlog.toml`).
* **Inspect before editing.** Ask it to read the existing module (router -> service -> repository/policy -> models),
  its tests and the relevant blueprint section first, and to follow the existing conventions and layering.
* **Tests are part of the change.** New behaviour needs tests that prove it; every bug fix needs a regression test that
  fails without the fix.
* **Demand factual summaries.** It must list the files it changed, the exact commands it ran and their real results.
  If it did not run something, it must say so.
* **Forbidden, whoever writes the code:** fabricated or assumed test results; weakening, skipping or deleting tests or
  assertions to get green; lowering coverage or disabling checks; TODO/placeholder/stub implementations or fake data;
  invented library APIs (check the docs or write a tiny experiment); bypassing the ledger, policy functions or module
  boundaries; unrelated changes.
* **Review the final diff yourself** (`git diff origin/main...HEAD`) and make sure you understand and can explain every
  change before you open the PR. Re-run the checks yourself; do not rely on the assistant's claim that they pass.
* **Never paste secrets or personal data into a prompt**: no credentials, tokens, `.env` contents, MFA secrets,
  production data, customer data or real account numbers.
* Do not add AI co-author trailers to commits (team decision); mention AI use in the PR description if relevant.

## 4. Pull request titles and descriptions

### Title

The PR title becomes the squash commit on `main`, so CI checks it (`.github/workflows/pr-title.yml`) and the check is
required. Format: `type(scope): summary` or `type: summary`, optionally `!` for a breaking change.

* Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`, `security`.
* Scope (optional): lowercase letters, digits and `. _ / -`.
* After `: ` the summary must start with a non-space character.

| Valid | Invalid |
|---|---|
| `feat(payments): add payee limits` | `Add payee limits` (no type) |
| `fix(auth): handle expired tokens` | `fix:handle expired tokens` (no space after the colon) |
| `docs(setup): clarify Windows prerequisites` | `Feat(auth): ...` (type must be lowercase) |
| `ci: enforce required workflow results` | `wip: half done` (unknown type) |
| `revert: restore previous validation` | `Revert "feat(auth): ..."` |

GitHub's **Revert** button creates a title like `Revert "feat(auth): ..."`, which fails the check. Rename it to
`revert: <what is restored>` before requesting review. Editing the title re-runs the check.

### Description

Fill in every section of the PR template:

* **Task:** link the issue. Use `Closes #NN` only when the PR completes every acceptance criterion: merging closes the
  issue and the board moves it to Done. For partial work, or a task that still needs other verification (for example a
  real Windows run), use `Refs #NN` instead.
* **Acceptance criteria table:** each criterion mapped to the test, file or artefact that proves it.
* **Summary**, **tests added or updated**, and **commands run with their actual output** (for example
  `make check` -> `401 passed, coverage 95.75%`). A ticked checkbox is a statement, not evidence; the CI checks on the
  PR are the evidence for automated checks.
* **Impact:** security, database migration, configuration, API contract, architecture.
* **Manual verification**, **known limitations and follow-up work** (with issue links).

Open the PR early as a draft if useful; mark it ready for review only when `CI Gate` and the PR-title check are green.

### Review

* Answer every review comment: fix it in a new commit on the same branch, or reply with your reasoning. Resolve each
  conversation (unresolved conversations block the merge).
* Pushing new commits re-runs CI and dismisses earlier approvals, so the latest version is always what gets reviewed.
* Contributors do not merge their own PRs; merging is done by the repository maintainer and is enforced by the
  repository rulesets.

## 5. Coding and testing practices

### Commands

| Purpose | Linux / macOS | Windows PowerShell |
|---|---|---|
| Fast tests while developing (no DB) | `make test-unit` | `.\dev.ps1 test-unit` |
| Focused tests (loads `.env` like CI's test env) | `python scripts/dev.py test --no-cov -k <name>` | `.\dev.ps1 test --no-cov -k <name>` |
| Python tests + coverage (needs `infra-up`) | `make test` | `.\dev.ps1 test` |
| Web tests | `make test-web` | `.\dev.ps1 test-web` |
| Lint + format check / auto-format | `make lint` / `make fmt` | `.\dev.ps1 lint` / `.\dev.ps1 fmt` |
| Types (mypy strict + tsc) | `make typecheck` | `.\dev.ps1 typecheck` |
| Everything CI runs, before review | `make check` | `.\dev.ps1 check` |

The pre-push hook runs mypy, import-linter, fast unit tests and the OpenAPI contract check. CI (`CI Gate`) additionally
runs integration and security tests on real PostgreSQL and Redis, migrations up -> down -> up -> `alembic check`, ML
tests, web lint/types/tests/build, Docker builds, Playwright E2E, Bandit, pip-audit, npm audit and detect-secrets.
CodeQL also analyses every PR.

### Standards

* **Python 3.12:** `mypy --strict` clean, `ruff` clean, no `Any` unless unavoidable and commented, no bare `except`,
  timezone-aware datetimes, no `print` (use `structlog` with keyword fields, no f-strings in logs).
* **TypeScript:** `strict` with `noUncheckedIndexedAccess`; no `any`, no non-null `!`, no `@ts-ignore`; data fetching via
  react-query; API types only from the generated `schema.d.ts`.
* **Coverage:** the Python gate is 80% (target 90%+ in `ledger`, `payments`, `auth`). Never lower it, exclude code to
  game it, or delete tests to pass it.
* **Tests:** deterministic only: inject the clock, seed RNGs, no sleeps, no network, real PostgreSQL for integration
  tests (never SQLite). A failing test is information: find the root cause, never weaken the assertion.

### Database migrations

Every schema change needs an Alembic migration with a working `upgrade` **and** `downgrade`:
```bash
uv run alembic -c apps/api/alembic.ini revision --autogenerate -m "short description"
# review and hand-edit the generated file (constraints, triggers, backfills), then:
uv run alembic -c apps/api/alembic.ini upgrade head
uv run alembic -c apps/api/alembic.ini downgrade -1
uv run alembic -c apps/api/alembic.ini upgrade head
uv run alembic -c apps/api/alembic.ini check
```
Never edit an applied migration (add a new one), never use `create_all()`, and register every new `models.py` in
`pbip_api/modules/__init__.py` and `alembic/env.py`.

### API changes

Business routes live under `/api/v1/...` (additive changes only; breaking changes need `/api/v2`). Every route needs
`response_model`, `summary`, tags and accurate status codes; errors are RFC 7807. After any API change run
`make openapi` and commit both `docs/api/openapi.json` and `apps/web/src/api/schema.d.ts`; CI fails if either is stale.
Never hand-edit generated files.

### Security-sensitive code (`AGENTS.md` section 5)

* **Authentication** on every route (`get_current_principal`, `require_roles`, `get_current_customer`); staff routes use
  `require_roles(..., mfa=True)`.
* **Object authorization**, not just roles: fetch customer data only through policy functions that take the
  authenticated customer id; never accept `customer_id`/`user_id` from the client; non-owned and non-existent both return
  404; add new endpoints to the BOLA matrix in `apps/api/tests/integration/test_rbac_bola.py`.
* **Input validation:** Pydantic models with bounds, enums and regexes; amounts are positive integers; path ids are UUIDs.
* **Logging and errors:** never log passwords, tokens, MFA codes or secrets, full account numbers, emails or request
  bodies; raise `DomainError` subclasses; never return stack traces, SQL or echoed user input.
* **Audit:** security-relevant state changes call `AuditService.record` in the same transaction.

### Money, ledger and authentication

* Money is integer minor units (`core/money.py`, `lib/money.ts`); never `float`.
* Only `modules/ledger/service.py` may change balances or insert ledger entries; corrections are compensating
  transactions.
* Respect the lock order (per-user advisory lock -> idempotency key -> accounts `FOR UPDATE` in sorted id order).
* Money-moving POST endpoints require `Idempotency-Key` and must be replay-safe.
* Changes in `ledger/`, `payments/`, `auth/`, `core/security.py` or `alembic/` need negative tests and concurrency tests:
  happy path, validation failure, another user's object, idempotent retry, and a concurrent attempt using real threads
  against real PostgreSQL.

### Never commit

`.env` files, passwords, MFA secrets, private keys, tokens, `.dev-secrets.txt` or other generated secret files,
production or customer data, or logs containing sensitive values. detect-secrets (pre-commit and CI), GitHub secret
scanning and push protection are backstops, not permission. If a secret is ever committed, report it immediately so it
can be rotated; deleting it in a later commit does not remove it from history.

## 6. Completion and documentation

* **Opening a PR** proposes a change. **Merging** puts it on `main` after CI and review. **Done** means the task's
  acceptance criteria and definition of done are verified with evidence, which can require more than a merge (for
  example a real Windows run). These are three different states.
* Board status: **In Progress** when you start, **In Review** when the PR is ready and green, **Done** only when merged
  and every acceptance criterion is verified. Never mark work Done early.
* Keep the issue, the execution plan and the board consistent. Record the actual completion date only when it happens;
  target deadlines and actual completion are separate facts.
* When your task merges, post on each downstream issue: the PR link, the interface/contract, the proving tests and any
  setup steps.
* Record blockers on the issue as soon as they appear (blocked more than half a working day: notify the owner you
  depend on). Report evidence honestly, including what you did not verify.
* Update documentation in the same PR: README or blueprint when behaviour or architecture changes, an ADR in
  `docs/adr/` for decisions with trade-offs, `docs/security/threat-model.md` for new attack surface. Architectural
  changes (new module, dependency, table, background job, cross-domain contract) need an ADR and agreement **before**
  implementation.

## Repository and GitHub setup (maintainers)

Labels, sprint milestones and the `main` ruleset can be configured with `scripts/setup_github.py`:
```bash
python scripts/setup_github.py <OWNER>/<REPO> --dry-run
python scripts/setup_github.py <OWNER>/<REPO> [--collaborators user1 user2] [--dry-run]
```
The live rulesets are committed in `.github/rulesets/`; the script imports `main.json` only (import
`main-merge-restriction.json` via Settings > Rules > Rulesets > Import). See blueprint section 14.
