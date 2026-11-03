"""Issue #315 (migration 0028): enum CHECKs, append-only security_events, FK indexes, CHECK names.

Each new constraint is proven to reject bad data with raw SQL (the ORM's own enum validation would
hide the database's behaviour), and to accept every value the application writes. The migration's
data check is proven by downgrading, inserting a row the CHECK would reject and upgrading again; the
fixture always returns the shared test database to head.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.models import SecurityEvent, SecurityEventType
from pbip_api.modules.ledger import service as ledger
from pbip_ml.fraud.contract import FraudDecision
from pbip_test_support import PASSWORD, Actor
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def alembic_config(migrated_engine: Engine, test_database_url: str) -> Iterator[Config]:
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    config = Config(str(ALEMBIC_INI))
    yield config
    command.upgrade(config, "head")


def _insert_security_event(engine: Engine, user_id: uuid.UUID | None, event_type: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO security_events (id, user_id, event_type, details) "
                "VALUES (:id, :user_id, :event_type, '{}')"
            ),
            {"id": event_id, "user_id": user_id, "event_type": event_type},
        )
    return event_id


def _insert_fraud_event(engine: Engine, actor: Actor, decision: str) -> None:
    assert actor.account_id is not None
    with Session(engine) as session:
        transaction_id = ledger.deposit(session, actor.account_id, 1_000).id
        session.commit()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fraud_events (id, transaction_id, customer_id, risk_score, decision, "
                "reasons, features, model_version, latency_ms) VALUES (:id, :tx, :customer, 0, "
                ":decision, '[]', '{}', 'test', 1.0)"
            ),
            {
                "id": uuid.uuid4(),
                "tx": transaction_id,
                "customer": actor.customer_id,
                "decision": decision,
            },
        )


def _insert_refresh_token(engine: Engine, user_id: uuid.UUID, amr: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, amr, expires_at) "
                "VALUES (:id, :user_id, :family, :hash, :amr, :expires)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "family": uuid.uuid4(),
                "hash": uuid.uuid4().hex,
                "amr": amr,
                "expires": utcnow() + timedelta(days=1),
            },
        )


def test_security_event_type_check_accepts_every_enum_value_and_rejects_others(
    migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    for event_type in SecurityEventType:
        _insert_security_event(migrated_engine, alice.user_id, event_type.value)
    with pytest.raises(IntegrityError, match="ck_security_events_event_type_valid"):
        _insert_security_event(migrated_engine, alice.user_id, "LOGIN_MAYBE")


def test_fraud_event_decision_check_accepts_every_decision_and_rejects_others(
    migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(funds=0)
    for decision in FraudDecision:
        _insert_fraud_event(migrated_engine, alice, decision.value)
    with pytest.raises(IntegrityError, match="ck_fraud_events_decision_valid"):
        _insert_fraud_event(migrated_engine, alice, "allow")


def test_refresh_token_amr_check_accepts_issued_methods_and_rejects_others(
    migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    for amr in ("pwd", "pwd,otp"):
        _insert_refresh_token(migrated_engine, alice.user_id, amr)
    for amr in ("otp", "pwd,otp,admin", ""):
        with pytest.raises(IntegrityError, match="ck_refresh_tokens_amr_valid"):
            _insert_refresh_token(migrated_engine, alice.user_id, amr)


def test_security_events_reject_update_and_delete(
    migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer()
    event_id = _insert_security_event(migrated_engine, alice.user_id, "LOGIN_FAILURE")
    for statement in (
        "UPDATE security_events SET event_type = 'LOGIN_SUCCESS' WHERE id = :id",
        "UPDATE security_events SET user_id = NULL WHERE id = :id",
        "DELETE FROM security_events WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"), migrated_engine.begin() as conn:
            conn.execute(text(statement), {"id": event_id})
    with Session(migrated_engine) as session:
        row = session.get(SecurityEvent, event_id)
        assert row is not None
        assert row.event_type == SecurityEventType.LOGIN_FAILURE


def test_the_real_sign_in_flows_still_write_security_events_and_rotate_tokens(
    client: TestClient, migrated_engine: Engine, make_customer: Callable[..., Actor]
) -> None:
    """Legitimate writers: registration, sign-in failure (committed before the error), sign-in,
    refresh rotation (updates refresh_tokens, never security_events) and sign-out."""
    alice = make_customer()
    bad = client.post("/api/v1/auth/login", json={"email": alice.email, "password": "wrong-pw-1"})
    assert bad.status_code == 401
    login = client.post("/api/v1/auth/login", json={"email": alice.email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    refresh = client.post("/api/v1/auth/refresh", headers={"X-Requested-With": "pbip-web"})
    assert refresh.status_code == 200, refresh.text
    with Session(migrated_engine) as session:
        kinds = set(
            session.execute(
                select(SecurityEvent.event_type).where(SecurityEvent.user_id == alice.user_id)
            ).scalars()
        )
    assert {
        SecurityEventType.USER_REGISTERED,
        SecurityEventType.LOGIN_FAILURE,
        SecurityEventType.LOGIN_SUCCESS,
        SecurityEventType.TOKEN_REFRESHED,
    } <= kinds


def test_every_foreign_key_has_an_index_leading_with_its_column(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        missing = conn.execute(
            text(
                "SELECT c.conrelid::regclass::text, c.conname FROM pg_constraint c "
                "WHERE c.contype = 'f' AND NOT EXISTS (SELECT 1 FROM pg_index i "
                "WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]) ORDER BY 1, 2"
            )
        ).all()
    assert missing == []


def test_no_check_constraint_name_repeats_its_table_prefix(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        doubled = conn.execute(
            text(
                "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                "WHERE contype = 'c' AND conname ~ '^ck_[a-z_]+_ck_'"
            )
        ).all()
    assert doubled == []


def test_upgrade_stops_loudly_when_existing_rows_break_a_new_check(
    migrated_engine: Engine, make_customer: Callable[..., Actor], alembic_config: Config
) -> None:
    alice = make_customer()
    command.downgrade(alembic_config, "0027")
    bad_id = _insert_security_event(migrated_engine, alice.user_id, "LEGACY_EVENT")
    try:
        with pytest.raises(RuntimeError, match=r"security_events: 1 row\(s\) violate"):
            command.upgrade(alembic_config, "0028")
        with migrated_engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            kept = conn.execute(
                text("SELECT event_type FROM security_events WHERE id = :id"), {"id": bad_id}
            ).scalar_one()
        assert version == "0027"  # nothing applied
        assert kept == "LEGACY_EVENT"  # and the data was not rewritten
    finally:
        # At 0027 there is no trigger yet, so the deliberate fix is possible.
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM security_events WHERE id = :id"), {"id": bad_id})
    command.upgrade(alembic_config, "head")
    with pytest.raises(DBAPIError, match="append-only"), migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM security_events"))
