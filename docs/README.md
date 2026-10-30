# Documentation index

| Deliverable | Location | Domain / Scope | Due | Status |
|---|---|---|---|---|
| Engineering blueprint (source of truth) | `docs/PROJECT_BLUEPRINT.md` | Team / Shared | S1 | **Done - keep current** |
| README / setup | `README.md` | Platform Core | S1 | **Done** |
| Requirements specification | `docs/requirements.md` | Team / Shared | S1 | Todo |
| User stories (GitHub Issues + export) | Issues, `docs/user-stories.md` | Team / Shared | S1-2 | Todo |
| Use cases (UC-01..UC-13 mapping) | `docs/use-cases.md` | Team / Shared | S2 | Todo |
| ER diagram | blueprint section 4 (+ `docs/er.md` as it grows) | Platform Core | S2 | Started |
| HLD | `docs/hld.md` | Platform Core | S4 | Todo |
| LLD (per module) | `docs/lld/*.md` | Platform / ML | S2-5 | Todo |
| API documentation | `docs/api/openapi.json` (generated) + `/docs` | Platform Core | always | **Auto** |
| Threat model / security | `docs/security/threat-model.md` | Security / Platform | S3, updated each sprint | v0.1 |
| ML / fraud methodology | `docs/ml/fraud-methodology.md` | Intelligence Layer | S3-5 | Todo |
| Model evaluation report | `docs/ml/evaluation-report.md` | Intelligence Layer | S6 | Todo |
| Deployment guide | `docs/deployment.md` | DevOps / Platform | S5 | Todo |
| Support / operations guide | `docs/operations/runbook.md` | Operations / Platform | S5 | **v1 (TASK-075)** |
| User manual | `docs/user-manual.md` | Team / Shared | S7 | Todo |
| Test strategy + report | blueprint section 12 -> `docs/test-report.md` | Quality / Testing | S6 | Strategy done |
| Architecture decision records | `docs/adr/` | Team / Shared | as decided | 10 accepted |
| Web UI guidelines and design system | `docs/design/ui-guidelines.md` (ADR-0016) | Frontend | before new screens | v1 |
| Team execution plan (owners, dates, handoffs) | `docs/TEAM_EXECUTION_PLAN.md` | Team / Shared | Oct 25 - Nov 3 2026 | v2 (Oct 25, hard Nov 3 deadline) |

Rule: a PR that changes behaviour updates the relevant doc in the same PR. Docs are never a week-14 activity.

## Requirements traceability

[`requirements/traceability.md`](requirements/traceability.md) maps every use case and non-functional requirement of the use case document (v0.2) to the code and the CI tests that prove it, with the gaps that remain.
