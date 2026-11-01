"""Feature modules of the modular monolith.

Layering inside every module (enforced by review, import-linter and AGENTS.md):

    router.py      HTTP only: parse, authenticate, call a service, shape the response
    service.py     use cases and transaction boundaries; calls policies and repositories
    policy.py      authorization decisions (object-level access control)
    repository.py  the only place that talks to the database for this module
    models.py      SQLAlchemy models        schemas.py   Pydantic request/response models

A module may call another module ONLY through that module's service interface, never by
importing its repository or models.
"""

# Register EVERY model with SQLAlchemy's metadata whenever any module is imported.
# Without this, entry points that do not load the whole web app (Celery worker, scripts,
# Alembic) fail with NoReferencedTableError because cross-module foreign keys cannot resolve.
# ADD NEW MODULES' models HERE.
from pbip_api.modules.accounts import models as _accounts  # noqa: F401
from pbip_api.modules.analytics import models as _analytics  # noqa: F401
from pbip_api.modules.assistant import models as _assistant  # noqa: F401
from pbip_api.modules.auth import models as _auth  # noqa: F401
from pbip_api.modules.bills import models as _bills  # noqa: F401
from pbip_api.modules.compliance import models as _compliance  # noqa: F401
from pbip_api.modules.customers import models as _customers  # noqa: F401
from pbip_api.modules.events import models as _events  # noqa: F401
from pbip_api.modules.fraud import models as _fraud  # noqa: F401
from pbip_api.modules.ledger import models as _ledger  # noqa: F401
from pbip_api.modules.notifications import models as _notifications  # noqa: F401
from pbip_api.modules.payees import models as _payees  # noqa: F401
from pbip_api.modules.payments import models as _payments  # noqa: F401
