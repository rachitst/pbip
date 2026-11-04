# Module and class diagrams

How the API is layered inside each module, which modules call which, and the package boundaries that
import-linter enforces in CI (`make imports`, the `[tool.importlinter]` contracts in `pyproject.toml`).
Paths are relative to `apps/api/src/pbip_api/`.

| Diagram | Source |
|---|---|
| [1. Layers inside a module](#1-layers-inside-a-module) | AGENTS.md section 2, `modules/payments/` as the worked example |
| [2. Module dependencies](#2-module-dependencies) | the `pbip_api.modules.*` imports of every module on `main` |
| [3. Import contracts](#3-import-contracts) | `pyproject.toml` `[[tool.importlinter.contracts]]` |

## 1. Layers inside a module

Exports: [SVG](svg/module-layers.svg), [PNG](png/module-layers.png).
Every module under `modules/` follows the same layers. Routers hold no business logic; services own the use case and
call `session.commit()` once at its end; customer data is read only through policy functions that take the
authenticated customer id. Not every module has every layer: `ledger` has no router (it is called by other services
only), `statements` and `recommendations` have no models, and `health` is a router only.

<!-- diagram: module-layers -->
```mermaid
classDiagram
    direction TB
    class router {
        <<router.py: HTTP only>>
        +APIRouter prefix /api/v1/transfers
        +Depends get_current_principal or require_roles
        +Pydantic request and response models
        +Idempotency-Key header
        +create_transfer()
        +confirm_step_up()
    }
    class service {
        <<service.py: use cases, owns the transaction>>
        +PaymentsService
        +create_transfer()
        +confirm_step_up()
        +review_alert()
        -_lock_user()
        -_begin_idempotent()
        +commit once at the end
    }
    class policy {
        <<policy.py: object authorization>>
        +get_owned_scheduled_transfer(session, customer_id, id)
        +non-owned and missing are both 404
    }
    class repository {
        <<repository.py: queries, where a module has one>>
        +read models and aggregates
    }
    class models {
        <<models.py: SQLAlchemy tables>>
        +ScheduledTransfer
        +ScheduledTransferStatus
        +CHECK constraints via in_check
    }
    class schemas {
        <<schemas.py: Pydantic contracts>>
        +TransferRequest
        +TransferOut
    }
    class other_module_service {
        <<another module's service or policy>>
        +ledger.service.post_transaction()
        +fraud scorer via FraudScorer contract
        +accounts.policy.get_owned_account()
        +AuditService.record()
    }
    router --> service : calls
    router ..> schemas : validates with
    service --> policy : authorizes through
    service --> repository : queries through
    service --> models : reads and writes
    policy --> models
    repository --> models
    service --> other_module_service : only service or policy functions
```

## 2. Module dependencies

Exports: [SVG](svg/module-dependencies.svg), [PNG](png/module-dependencies.png).
An arrow means "imports from". Two dependencies are used by almost every module and are drawn once, as the shared
box at the bottom: `auth.dependencies` (the authenticated principal and role gates) and `compliance.service`
(`AuditService.record`, called inside the use case's transaction). Only `modules/ledger/service.py` changes balances or
writes entries; every money-moving module goes through it.

<!-- diagram: module-dependencies -->
```mermaid
flowchart LR
    subgraph money["Money movement"]
        payments["payments<br/>transfers, step-up, scheduled"]
        bills["bills"]
        loans["loans"]
        wealth["wealth<br/>deposits, insurance catalogue"]
        ledger[("ledger<br/>the only writer of balances and entries")]
    end
    subgraph risk["Fraud and staff"]
        fraud["fraud<br/>scorers, alerts, monitoring"]
        compliance["compliance<br/>audit log, exceptions"]
        insights["insights<br/>RM views, behaviour"]
        runtime["runtime_config"]
    end
    subgraph customer["Customer data"]
        accounts["accounts"]
        payees["payees"]
        customers["customers"]
        statements["statements"]
        notifications["notifications<br/>outbox and in-app"]
    end
    subgraph intel["Analytics and assistant"]
        analytics["analytics"]
        recommendations["recommendations"]
        assistant["assistant"]
        events["events<br/>clickstream"]
    end
    shared["auth.dependencies and compliance.service<br/>(used by almost every module)"]
    auth["auth<br/>sessions, MFA, admin users"]

    payments --> ledger
    payments --> fraud
    payments --> payees
    payments --> accounts
    payments --> loans
    payments --> notifications
    payments --> runtime
    bills --> ledger
    bills --> fraud
    bills --> payments
    bills --> accounts
    bills --> notifications
    loans --> ledger
    loans --> payments
    loans --> analytics
    loans --> accounts
    wealth --> ledger
    wealth --> payments
    wealth --> accounts
    wealth --> customers
    statements --> ledger
    statements --> accounts
    statements --> notifications
    fraud --> payments
    fraud --> bills
    fraud --> ledger
    fraud --> accounts
    fraud --> payees
    compliance --> fraud
    compliance --> ledger
    insights --> analytics
    insights --> recommendations
    insights --> accounts
    analytics --> accounts
    analytics --> loans
    analytics --> events
    analytics --> payments
    analytics --> notifications
    recommendations --> analytics
    recommendations --> loans
    recommendations --> wealth
    recommendations --> customers
    assistant --> accounts
    assistant --> analytics
    assistant --> loans
    assistant --> wealth
    assistant --> recommendations
    assistant --> statements
    assistant --> payments
    assistant --> runtime
    assistant --> customers
    accounts --> ledger
    payees --> accounts
    auth --> accounts
    auth --> notifications
    customers --> notifications
    events --> customers
    ledger --> accounts
    money --> shared
    risk --> shared
    customer --> shared
    intel --> shared
    shared --> auth
```

## 3. Import contracts

Exports: [SVG](svg/module-import-contracts.svg), [PNG](png/module-import-contracts.png).
The five import-linter contracts that CI enforces (`API - lint, types, boundaries, contract` job). A crossed arrow is a
forbidden import, checked transitively; its label names the contract in the table below.

<!-- diagram: module-import-contracts -->
```mermaid
flowchart TB
    subgraph api["pbip_api"]
        core["pbip_api.core<br/>config, db, security, money, metrics"]
        modules["pbip_api.modules.*"]
        worker["pbip_api.worker<br/>Celery tasks"]
    end
    subgraph ml["pbip_ml"]
        fserve["fraud serving<br/>contract, features, artifacts,<br/>hybrid, inference, rules"]
        ftrain["fraud training<br/>train, training, evaluation, promotion"]
        rserve["recommendation serving<br/>contract, engine, propensity"]
        rtrain["recommendation training<br/>panel, train, training"]
        other["features, forecasting,<br/>segmentation, monitoring"]
    end
    datagen["pbip_datagen<br/>synthetic data and simulated uptake labels"]

    modules --> core
    worker --> modules
    modules --> fserve
    modules --> rserve
    modules --> other
    ftrain --> fserve
    rtrain --> rserve

    core -- "C1 forbidden" --x modules
    ml -- "C2 forbidden" --x api
    datagen -- "C2 forbidden" --x api
    api -- "C3 forbidden" --x ftrain
    fserve -- "C3 forbidden" --x ftrain
    api -- "C4 forbidden" --x rtrain
    rserve -- "C4 forbidden" --x rtrain
    datagen -- "C5 forbidden" --x ml
```

| | Contract name in `pyproject.toml` | Source | Forbidden |
|---|---|---|---|
| C1 | core must not depend on feature modules | `pbip_api.core` | `pbip_api.modules` |
| C2 | ml package must not depend on the API | `pbip_ml`, `pbip_datagen` | `pbip_api` |
| C3 | fraud serving must not depend on fraud training | `pbip_api` and the fraud serving modules | `pbip_ml.fraud.evaluation`, `.promotion`, `.train`, `.training` |
| C4 | recommendation serving must not depend on recommendation training | `pbip_api` and the recommendation serving modules | `pbip_ml.recommendations.panel`, `.train`, `.training` |
| C5 | simulated uptake labels must not depend on the recommendation rules | `pbip_datagen` | `pbip_ml` |

The layering inside a module (diagram 1) and the "service or policy only" rule between modules are review rules from
AGENTS.md section 2, not import-linter contracts. The route policy registry test
(`apps/api/tests/security/test_route_policy_registry.py`) enforces that every route is authenticated.

**Extension point.** A new module is a new node in diagram 2 with one arrow per module it imports; a new
import-linter contract is a new crossed arrow in diagram 3 and a row in the table.
