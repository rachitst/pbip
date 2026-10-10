## What and why
<!-- One paragraph. Link the issue: Closes #123 -->

## Type
- [ ] feat  - [ ] fix  - [ ] refactor  - [ ] docs  - [ ] test  - [ ] chore/ci  - [ ] security

## Checklist (all must be true before requesting review)
- [ ] `make check` passes locally
- [ ] New/changed behaviour has tests that prove it (bug fix => regression test that fails without the fix)
- [ ] DB schema change => Alembic migration included and `alembic upgrade/downgrade` tested
- [ ] API change => `make openapi` run and the updated `docs/api/openapi.json` + web types committed
- [ ] Every new endpoint authenticates (`get_current_principal`) and authorizes the OBJECT (ownership via a policy function), not just the role
- [ ] No secrets, no sensitive data in logs, all external input validated
- [ ] Docs updated (README / blueprint / ADR if architecture changed)

## Architectural impact
<!-- "None" or explain: new module, new dependency, changed contract between modules/domains, etc. -->

## How to verify
<!-- Commands / screenshots / demo script -->
