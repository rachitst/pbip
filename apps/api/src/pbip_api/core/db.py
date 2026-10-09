"""Database engine, session management and shared model helpers.

Synchronous SQLAlchemy 2.0 + psycopg 3. Transaction boundaries belong to the SERVICE layer
(one use case = one transaction): services call ``session.commit()``; routers and
repositories never do. A request that raises leaves the transaction uncommitted, so it is
rolled back when the session closes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from uuid import UUID, uuid4

from fastapi import Depends
from sqlalchemy import CheckConstraint, DateTime, Engine, Enum, MetaData, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from pbip_api.core.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> UUID:
    return uuid4()


def uuid_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4, server_default=text("gen_random_uuid()"))


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def str_enum(enum_cls: type[StrEnum], length: int = 32) -> Enum:
    """Store a StrEnum as VARCHAR (not a native PG enum, which is painful to migrate)."""
    return Enum(enum_cls, native_enum=False, length=length, validate_strings=True)


def in_check(column: str, enum_cls: type[StrEnum], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10)


def get_session(engine: Engine = Depends(get_engine)) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Session for non-request code (Celery tasks, scripts). Commits on success."""
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
