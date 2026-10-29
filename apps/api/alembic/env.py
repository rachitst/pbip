"""Alembic environment. The URL comes from settings (DATABASE_URL or ALEMBIC_DATABASE_URL)."""

import os
from logging.config import fileConfig

from alembic import context
from pbip_api.core.config import get_settings
from pbip_api.core.db import Base
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Import every module's models so autogenerate sees them. ADD NEW MODULES HERE.
from pbip_api.modules.accounts import models as _accounts  # noqa: E402, F401
from pbip_api.modules.analytics import models as _analytics  # noqa: E402, F401
from pbip_api.modules.auth import models as _auth  # noqa: E402, F401
from pbip_api.modules.compliance import models as _compliance  # noqa: E402, F401
from pbip_api.modules.customers import models as _customers  # noqa: E402, F401
from pbip_api.modules.fraud import models as _fraud  # noqa: E402, F401
from pbip_api.modules.ledger import models as _ledger  # noqa: E402, F401
from pbip_api.modules.notifications import models as _notifications  # noqa: E402, F401
from pbip_api.modules.payees import models as _payees  # noqa: E402, F401

target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
