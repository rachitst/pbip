# Private-repository readiness checklist

| | |
|---|---|
| Repository | `rachitst/pbip` (owned by a personal account, currently **public**) |
| Checked | 2026-11-03, against `main` at `ceb2a92` and GitHub's documentation on that date |
| Account plan | **Unknown**: `gh api user --jq .plan` returns `null` for this token, so the plan is not visible here. The owner can see it under Settings > Billing and licensing. The sections below give the outcome for GitHub Free and for GitHub Pro. |

The owner makes the repository private **after** the project is finished. Nothing in this document changes
visibility, settings or rulesets; it lists what changes when the switch happens and what to do about it.

## 1. Secrets over the full git history

**Result: no real secret. Nothing needs rotating.**

* gitleaks `v8.21.2` (official container, `git` mode, `--log-opts=--all`, `--redact`) over all 245 commits:
  18 findings, all test-only dummies or hashes:

  | Finding | Where (rule) | Why it is not a secret |
  |---|---|---|
  | 14 x | `.secrets.baseline` (generic-api-key) | SHA-1 `hashed_secret` values of already-audited dummies, not secrets |
  | 3 x | `scripts/smoke_test.py`, `scripts/smoke_test.sh` (generic-api-key; commits `213f8aa`, `c1934af`, `bea81c7`) | A deliberately wrong password sent to `/auth/login` to prove a 401 |
  | 1 x | `apps/api/tests/unit/test_logging_redaction.py` (jwt; commit `213f8aa`) | A hand-made JWT-shaped string (signature text `signature123`) that tests log redaction |

  The new [`.gitleaks.toml`](../../.gitleaks.toml) extends the default rules and allow-lists exactly these three
  patterns, each with its reason. With it, the same full-history scan and a scan of the working tree report
  **no leaks**.
* detect-secrets (the CI step `git ls-files ... | detect-secrets-hook --baseline .secrets.baseline`): passes.
  The 15 baseline entries were all reviewed and are now marked `"is_secret": false` (audited):

  | File | Type | Reason |
  |---|---|---|
  | `.env.example`, `apps/api/src/pbip_api/core/config.py:25`, `apps/api/tests/conftest.py` | Basic Auth | The local-only PostgreSQL URL (`pbip_local_password`); staging/production refuse to boot with dev values |
  | `.github/workflows/ci.yml` (x2) | Secret Keyword / Basic Auth | Throw-away password of the CI service container |
  | `apps/api/src/pbip_api/core/config.py:14` | Secret Keyword | `local-dev-only-change-me...` JWT secret, rejected outside `APP_ENV=local` (tested) |
  | `apps/api/src/pbip_api/modules/auth/models.py` | Secret Keyword | An audit event NAME (`LOGIN_PASSWORD_OK_MFA_PENDING`), not a value |
  | `apps/api/tests/integration/test_auth.py` (x3), `apps/api/tests/pbip_test_support.py`, `apps/api/tests/unit/test_logging_redaction.py` (x2) | Secret Keyword / JWT | Test passwords and redaction fixtures |
  | `demo/demo_scenarios.py`, `scripts/seed_dev.py` | Secret Keyword | Demo password for the local seeded accounts (`APP_ENV=local` only) |

## 2. Personal and machine details

* **Tracked files (current tree): none found.** No absolute home paths (`/home/...`, `C:\Users\...`,
  `/Users/...`), no gist links, no personal e-mail addresses (only `@example.com`, `@demo.pbip`, `@bank.example`,
  `@evil.example`, `@perf.pbip` test domains), no real hostnames or IPs (only RFC 1918 addresses in GeoIP,
  rate-limit and audit-log tests). The GitHub handle `rachitst` (CODEOWNERS, script tests, CONTRIBUTING) and the
  team's first names in `docs/TEAM_EXECUTION_PLAN.md` are intentional project metadata.
* Fixed in this change: `docs/SETUP_GUIDE.md` cloned from `https://github.com/rachitst/pbip.git` in the Windows
  checklist; it now uses the same `<your-org-or-username>` placeholder as the rest of the guide.
* **History (left unrewritten, by policy):** no home paths or private links in any diff. The commit metadata
  contains the author/committer e-mail addresses of the contributors: personal Gmail addresses of four people,
  one college (`tpoly.in`) address, and otherwise `users.noreply.github.com`. Making the repo private hides them
  from the public; forks and clones that already exist keep them. Contributors who want to avoid this in future
  should enable "Keep my email addresses private" and use their noreply address.

## 3. Links that break once the repository is private

Private-repository URLs return 404 to anyone without access, so every `github.com/rachitst/pbip/...` link,
`raw.githubusercontent.com` link and badge stops working for outside readers.

| Location | Link | Action |
|---|---|---|
| `README.md` | No badges, no `raw.githubusercontent.com` or issue links | None needed |
| `docs/SETUP_GUIDE.md` | Clone URL `github.com/rachitst/pbip.git`; bare `#12`, `#23`, `#24`, `#132`-`#138` | Placeholder clone URL; references now read "GitHub issue #N" / "GitHub PR #N" |
| `docs/architecture/LLD.md` | `#50`, `#316` | Now "GitHub issue #N" |
| `docs/ml/fraud-models.md`, `docs/ml/model-evaluation-report-v1.md`, `docs/ml/model-cards/hybrid-fraud-combiner.md`, `docs/ml/model-cards/lightgbm-fraud.md` | `#251`, `#274`, `#275` | Now "GitHub issue #N" |
| `docs/architecture/HLD.md` | `#76`, `#198`, `#328` and others (8 references) | Not edited (frozen in the final sprint); same wording fix for the batch docs PR |
| `CONTRIBUTING.md` | Clone URL; Project board `github.com/users/rachitst/projects/1` | Kept: written for collaborators, who keep access. The board's own visibility is a separate project setting |
| `docs/TEAM_EXECUTION_PLAN.md` | Project board link | Kept (frozen file; collaborators only) |
| `.github/PULL_REQUEST_TEMPLATE.md`, `SETUP_GUIDE` "Resolves #12" example | Issue syntax inside GitHub | Kept: only rendered on GitHub |

There is no user manual in the repository yet (`docs/user-manual.md` is "Todo" in `docs/README.md`).

## 4. What changes on GitHub when the repository goes private

### 4.1 Rulesets (`.github/rulesets/`: `main`, `main-merge-restriction`)

> "Rulesets are available in public repositories with GitHub Free and GitHub Free for organizations, and in
> public and private repositories with GitHub Pro, GitHub Team, and GitHub Enterprise Cloud."
> ([About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets))

The live rulesets (`gh api repos/rachitst/pbip/rulesets`) are `main` (id 24846307: no deletion, no force-push,
linear history, PR required with squash only and resolved threads, required checks **`CI Gate`** and
**`PR title - Conventional Commit`**, strict) and `main-merge-restriction` (id 25838358: `update` rule, only the
admin role may merge via PR). **No CodeQL check is required** by either ruleset.

* **On GitHub Free: risk.** The rulesets stay stored but are not enforced on a private repository. Anyone with
  write access (six collaborators today) could push to `main` directly, force-push or merge with a red CI Gate.
  Protected branches have the same restriction
  ([About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)).
  Settings change: upgrade the owner account to **GitHub Pro** before switching, or remove write access from
  collaborators (Settings > Collaborators, set them to Read) once development stops.
* **On GitHub Pro:** both rulesets keep working unchanged. Pro also covers code owners and required reviewers in
  private repositories ([GitHub's plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)).
* After the switch, verify: Settings > Rules > Rulesets shows both as **Active**, and a test PR shows the
  required checks.

### 4.2 CodeQL and code scanning

> "If you want to use code scanning on private repositories, you need a GitHub Code Security license."
> ([About code scanning](https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning))

* `codeql.yml` already guards the job with `if: ${{ !github.event.repository.private }}`, so on a private
  repository it is **skipped** (no failure, no minutes). Because no ruleset requires a CodeQL check, merges are not
  blocked. Existing code-scanning alerts stop updating.
* Risk: the SAST coverage that remains is the CI `Security - SAST, dependencies, secrets` job (bandit,
  pip-audit, npm audit, detect-secrets), which is part of `CI Gate`. Buying Code Security is the only way to keep
  CodeQL on a private repository; if it is bought, remove the `if:` guard.

### 4.3 Secret scanning and push protection

Today `secret_scanning` and `secret_scanning_push_protection` are **enabled** (`gh api repos/rachitst/pbip`).
Secret scanning is listed for public repositories ("runs automatically for free"), organization-owned private
repositories with GitHub Secret Protection, and user-owned repositories only on Enterprise Cloud with Enterprise
Managed Users ([About secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning)).
**Risk:** on this personal-account repository both stop when it goes private, whatever the plan. Remaining
controls: the detect-secrets pre-commit hook and CI step, plus `.gitleaks.toml` for manual full-history scans
(`docker run --rm -v "$PWD":/repo:ro zricethezav/gitleaks:v8.21.2 git /repo --log-opts=--all --redact`).

### 4.4 GitHub Actions minutes

> "GitHub Actions usage is free for standard GitHub-hosted runners in public repositories ... For private
> repositories, each GitHub account receives storage and a monthly included compute usage, depending on the
> account's plan."
> ([Billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage))

Included compute for private repositories: **GitHub Free $12/month, GitHub Pro $18/month**
([GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[GitHub's plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)). At the standard
Linux 2-core rate of $0.006/minute
([Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)) that is about **2,000 minutes (Free)** or **3,000 minutes (Pro)**.

Measured from the run history (`gh run list` start/end times and per-job times from the jobs API; each job is
rounded up to a whole minute here as a conservative assumption):

| Workflow | Trigger | Typical billable minutes per run | Per month | Worst case (timeout) |
|---|---|---|---|---|
| `perf.yml` | nightly `30 1 * * *` | 6 | 180 | 45 x 30 = 1,350 |
| `fuzz.yml` | nightly `0 2 * * *` | 2 | 60 | 25 x 30 = 750 |
| `dast.yml` | nightly `15 2 * * *` | 3 | 90 | 30 x 30 = 900 |
| `codeql.yml` | weekly `17 3 * * 1` | 0 when private (skipped) | 0 | 0 |
| **Scheduled total** | | | **~330 (~$2)** | **~3,000 (~$18)** |
| `perf.yml` | push to `main` (paths) | 6 | ~210 (35 runs in the last 30 days) | |
| `ci.yml` | each PR and push to `main` | 24 (PR), 31 (push); 9 jobs | ~9,000-15,000 at the last 30 days' rate (588 runs) | |
| Dependabot PRs | weekly, 5 ecosystems | each PR runs CI | ~800-1,000 (31 Dependabot PRs since 2026-10-10) | |

* **After the project is finished** (scheduled jobs only), usage is about 330 minutes, well inside Free.
  The worst case (every nightly hitting its timeout) is about 3,000 minutes and would exceed Free.
* **Active development is not affordable on a private repository**: the last 30 days' CI volume is 3-7x the Pro
  allowance. If development continues after the switch, set a spending limit (Settings > Billing and licensing >
  Budgets) or reduce triggers.
* Settings changes once the project ends: disable the nightly `perf`, `fuzz` and `dast` workflows (Actions >
  workflow > "Disable workflow") unless their evidence is still needed, and check that the Actions budget stops
  usage at the amount you accept (for example $0) instead of billing overage.

### 4.5 GHCR images for the staging deploy

`deploy.yml` (`package` job) pushes `ghcr.io/rachitst/pbip/api` and `.../web` with `GITHUB_TOKEN`
(`packages: write`); `infra/deploy/deploy.sh` runs `docker compose pull` on the staging host **without a
`docker login`**. `deploy.yml` has never run (0 runs), so no package exists yet.

> "the default visibility is private and only you can see the package" ... "the package automatically inherits
> the access permissions (but not the visibility) of the linked repository."
> ([Configuring a package's access control and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility))

* **Risk:** the first release creates **private** packages whatever the repository visibility, so the staging
  pull fails with `unauthorized` and the deploy job fails. This is true before and after the switch.
* Settings change: on the staging host, run `docker login ghcr.io -u <owner>` once with a classic token that has
  only `read:packages`, kept in the host's Docker credential store (never in the repository), and document that
  step in the runbook (separate PR). Making the packages public is not recommended once the code is private
  ("Once you make a package public, you cannot make it private again").
* Private package storage counts against the plan (Free 500 MB, Pro 2 GB;
  [GitHub Packages billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages)); delete
  old `latest`/`vX.Y.Z` versions you no longer roll back to.

### 4.6 Dependabot

* Dependabot alerts are included in GitHub Free
  ([GitHub's plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)). Security updates
  need the dependency graph and Dependabot alerts enabled
  ([About Dependabot security updates](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates)).
  After the switch check Settings > Advanced Security
  that **Dependency graph**, **Dependabot alerts** and **Dependabot security updates** are still **Enabled**
  (`dependabot_security_updates` is enabled today).
* Version updates (`.github/dependabot.yml`, weekly, 5 ecosystems) keep working; each Dependabot PR runs CI and
  now costs minutes (section 4.4). Settings change after the project ends: set `schedule.interval: monthly`, or
  `open-pull-requests-limit: 0` for version updates while keeping security updates.

## 5. Order of operations for the owner

1. Decide the plan (Free or Pro) using sections 4.1 and 4.4.
2. Set an Actions budget (Settings > Billing and licensing > Budgets).
3. Disable or slow the nightly workflows and Dependabot version updates if the project is finished.
4. Switch visibility (Settings > General > Danger Zone > Change visibility > Make private).
5. Verify: rulesets Active (Pro), Dependabot settings Enabled, a test PR runs `CI Gate`, CodeQL is skipped.
6. Before the next release, give the staging host read access to GHCR (section 4.5).
