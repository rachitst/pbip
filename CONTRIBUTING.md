# Contributing

Core rule: **nothing reaches `main` without peer review and a green CI Gate.**

1. Pick/create a GitHub Issue (template: feature or bug). Assign yourself; label `area:*` and `tier:*`; set the sprint milestone.
2. Branch from fresh `main`: `git switch main && git pull && git switch -c feat/<short-name>` (`fix/`, `docs/`, `chore/`).
3. Work in small steps. Tests with every change; regression test with every bug fix; migration with every schema change;
   `make openapi` with every API change. Read the security checklist in `AGENTS.md` section 5.
4. Commit with **Conventional Commits** - the hook rejects anything else:
   `feat(payments): add scheduled transfers` - types: feat fix docs style refactor perf test build ci chore revert security.
5. `make check` locally, then push and open a PR using the template. Describe architectural impact honestly.
6. The reviewer checks: behaviour, tests (do they fail without the change?), authorization of the *object*, migration
   safety, logging hygiene, docs. Resolve every conversation. Squash-merge (the PR title becomes the commit message).
7. Never use `--no-verify`, never push to `main`, never force-push a shared branch, never merge on red.

Review SLA: respond within one working day; keep PRs under ~400 lines so that is realistic.
Knowledge sharing: regularly conduct cross-domain walkthroughs so all contributors understand the entire codebase.
