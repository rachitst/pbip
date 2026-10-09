# Documentation index

| Deliverable | Location | Owner | Due | Status |
|---|---|---|---|---|
| Engineering blueprint (source of truth) | `docs/PROJECT_BLUEPRINT.md` | both | S1 | **Done - keep current** |
| README / setup | `README.md` | A | S1 | **Done** |
| Requirements specification | `docs/requirements.md` | both | S1 | Todo |
| User stories (GitHub Issues + export) | Issues, `docs/user-stories.md` | both | S1-2 | Todo |
| Use cases (UC-01..UC-13 mapping) | `docs/use-cases.md` | both | S2 | Todo |
| ER diagram | blueprint section 4 (+ `docs/er.md` as it grows) | A | S2 | Started |
| HLD | `docs/hld.md` | A | S4 | Todo |
| LLD (per module) | `docs/lld/*.md` | A (core) / B (ML) | S2-5 | Todo |
| API documentation | `docs/api/openapi.json` (generated) + `/docs` | A | always | **Auto** |
| Threat model / security | `docs/security/threat-model.md` | A | S3, updated each sprint | v0.1 |
| ML / fraud methodology | `docs/ml/fraud-methodology.md` | B | S3-5 | Todo |
| Model evaluation report | `docs/ml/evaluation-report.md` | B | S6 | Todo |
| Deployment guide | `docs/deployment.md` | A | S5 | Todo |
| Support / operations guide | `docs/operations.md` | A | S5 | Todo |
| User manual | `docs/user-manual.md` | both | S7 | Todo |
| Test strategy + report | blueprint section 12 -> `docs/test-report.md` | A | S6 | Strategy done |
| Architecture decision records | `docs/adr/` | both | as decided | 7 seeded |

Rule: a PR that changes behaviour updates the relevant doc in the same PR. Docs are never a week-14 activity.
