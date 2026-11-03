"""Database access for runtime overrides. Only this module's service calls it."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from pbip_api.modules.runtime_config.models import RuntimeSetting


def list_overrides(session: Session) -> list[RuntimeSetting]:
    return list(session.execute(select(RuntimeSetting)).scalars().all())


def get_override(session: Session, key: str) -> RuntimeSetting | None:
    return session.execute(
        select(RuntimeSetting).where(RuntimeSetting.key == key)
    ).scalar_one_or_none()
