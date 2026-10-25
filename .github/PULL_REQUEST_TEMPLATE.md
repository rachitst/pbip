<!--
PR title = the squash commit on main: Conventional Commit, e.g. `feat(payments): add payee limits` (checked by CI).
Ticking a box is a statement by you, not evidence. The CI checks on this PR are the evidence for automated
checks; paste real command output for anything CI does not run. Leave a box unticked and explain if it does not hold.
-->

## Task and acceptance criteria
<!-- Closes #<issue> (TASK-xxx). Copy the task's acceptance criteria from the issue and say how each is met. -->
Closes #

| Acceptance criterion | How it is met (test name, file, or evidence) |
|---|---|
|  |  |

## Summary of changes
<!-- What changed and why, in a few sentences. -->

## Tests added or updated
<!-- Test files and test names. Bug fix: name the regression test that fails without the fix. -->

## Commands run and results
<!-- Exact commands and their real result, e.g. `make check` -> "395 passed, coverage 95.75%". Say what you did NOT run. -->
```text

```

## Impact
- **Security** (authn/authz, object ownership/BOLA, validation, logging, rate limits; AGENTS.md section 5):
- **Database migration** (Alembic up and down; `alembic check`):
- **Configuration / environment variables:**
- **API contract** (`make openapi`; `docs/api/openapi.json` and `apps/web/src/api/schema.d.ts` committed):
- **Architecture** (new module, dependency, table, background job or cross-domain contract; needs an ADR and prior approval):

## Manual verification
<!-- Steps you performed and what you saw: Swagger calls, screenshots, a second user for BOLA, Windows runs, etc. -->

## Known limitations and follow-up work
<!-- "None", or what is left and the issue tracking it. No TODO/placeholder code is allowed in the diff. -->

## Author confirmation
- [ ] The diff contains no secrets, credentials, tokens, private keys or `.env` files, and no sensitive data in logs.
- [ ] I ran the commands listed above myself and pasted their actual results.
- [ ] Every behaviour change has a test, and no existing test was weakened, skipped or deleted to pass CI.
- [ ] The diff contains only changes for this task (no unrelated files or drive-by refactors).
- [ ] Docs updated where behaviour or architecture changed (README, blueprint, ADR, threat model).
