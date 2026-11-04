"""#175 (migration 0029): the never-used FEE transaction type is gone from the ledger's type CHECK.

The CHECK is proven with raw SQL (the ORM's enum would hide the database's behaviour). The
migration's data check is proven by downgrading, inserting a FEE row and upgrading again; the
fixture always returns the shared test database to head.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pbip_api.modules.ledger.models import TransactionType
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def alembic_config(migrated_engine: Engine, test_database_url: str) -> Iterator[Config]:
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    config = Config(str(ALEMBIC_INI))
    yield config
    command.upgrade(config, "head")


def _insert_transaction(engine: Engine, tx_type: str) -> uuid.UUID:
    """A DECLINED header: no entries, so only the type CHECK is exercised."""
    tx_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO transactions (id, type, status, amount, currency, channel) "
                "VALUES (:id, :type, 'DECLINED', 100, 'INR', 'WEB')"
            ),
            {"id": tx_id, "type": tx_type},
        )
    return tx_id


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def test_the_ledger_rejects_fee_and_still_accepts_every_type_the_application_writes(
    migrated_engine: Engine,
) -> None:
    assert "FEE" not in {t.value for t in TransactionType}
    for tx_type in TransactionType:
        _insert_transaction(migrated_engine, tx_type.value)
    with pytest.raises(IntegrityError, match="ck_transactions_type_valid"):
        _insert_transaction(migrated_engine, "FEE")


def test_downgrade_restores_fee_in_the_check(
    migrated_engine: Engine, alembic_config: Config
) -> None:
    command.downgrade(alembic_config, "0028")
    fee_id = _insert_transaction(migrated_engine, "FEE")  # accepted again at 0028
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": fee_id})
    command.upgrade(alembic_config, "head")
    with pytest.raises(IntegrityError, match="ck_transactions_type_valid"):
        _insert_transaction(migrated_engine, "FEE")


def test_upgrade_stops_loudly_when_a_fee_transaction_exists(
    migrated_engine: Engine, alembic_config: Config
) -> None:
    command.downgrade(alembic_config, "0028")
    fee_id = _insert_transaction(migrated_engine, "FEE")
    try:
        with pytest.raises(RuntimeError, match=r"1 transaction\(s\) have type FEE"):
            command.upgrade(alembic_config, "0029")
        assert _version(migrated_engine) == "0028"  # nothing applied
        with migrated_engine.connect() as conn:
            kept = conn.execute(
                text("SELECT type FROM transactions WHERE id = :id"), {"id": fee_id}
            ).scalar_one()
        assert kept == "FEE"  # and the ledger row was not rewritten
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": fee_id})
