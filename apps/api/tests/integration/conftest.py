"""Integration fixtures: real PostgreSQL (migrated with Alembic), fakeredis, fake clock."""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fakeredis import FakeRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine
from pbip_api.core.redis import get_redis
from pbip_api.core.security import PasswordService
from pbip_api.main import create_app
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.dependencies import get_clock
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_test_support import PASSWORD, Actor, FakeClock, login_json, totp_code
from pydantic import SecretStr
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def migrated_engine(db_engine: Engine, test_database_url: str) -> Engine:
    with db_engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("DROP SCHEMA IF EXISTS synthetic CASCADE"))  # migration 0002
        connection.execute(text("CREATE SCHEMA public"))
    os.environ["ALEMBIC_DATABASE_URL"] = test_database_url
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return db_engine


@pytest.fixture(autouse=True)
def clean_db(migrated_engine: Engine) -> None:
    """Reset all mutable data between tests. TRUNCATE bypasses the append-only row triggers."""
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE users, user_roles, refresh_tokens, mfa_recovery_codes, security_events, "
                "customers, devices, account_holders, payees, transactions, transaction_entries, "
                "holds, idempotency_keys, fraud_events, fraud_alerts, audit_logs, notifications, "
                "outbox_events, monthly_category_spending, customer_segments CASCADE"
            )
        )
        connection.execute(text("DELETE FROM accounts WHERE account_type <> 'SYSTEM'"))
        connection.execute(text("UPDATE accounts SET ledger_balance = 0, held_amount = 0"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="local",
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
        jwt_secret=SecretStr("t" * 48),
        field_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.fixture
def app(
    migrated_engine: Engine, settings: Settings, fake_redis: FakeRedis, clock: FakeClock
) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_engine] = lambda: migrated_engine
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_redis] = lambda: fake_redis
    application.dependency_overrides[get_clock] = lambda: clock
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def auth_service_factory(
    migrated_engine: Engine, settings: Settings, clock: FakeClock
) -> Callable[[Session], AuthService]:
    def build(session: Session) -> AuthService:
        return AuthService(
            session,
            settings,
            PasswordService.from_settings(settings),
            FieldCipher(settings.field_encryption_key.get_secret_value()),
            clock,
        )

    return build


@pytest.fixture
def make_customer(client: TestClient, migrated_engine: Engine) -> Callable[..., Actor]:
    def factory(email: str | None = None, funds: int = 0) -> Actor:
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Test Customer"},
        )
        assert response.status_code == 201, response.text
        user_id = uuid.UUID(response.json()["id"])
        token = login_json(client, email)["access_token"]
        with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
            customer = session.execute(
                select(Customer).where(Customer.user_id == user_id)
            ).scalar_one()
            account = list_accounts(session, customer.id)[0]
            if funds:
                ledger.deposit(session, account.id, funds)
                session.commit()
            return Actor(
                email=email,
                user_id=user_id,
                access_token=token,
                customer_id=customer.id,
                account_id=account.id,
                account_number=account.account_number,
            )

    return factory


@pytest.fixture
def make_staff(
    client: TestClient,
    migrated_engine: Engine,
    auth_service_factory: Callable[[Session], AuthService],
    clock: FakeClock,
) -> Callable[..., Actor]:
    def factory(role: RoleName, mfa: bool = True) -> Actor:
        email = f"{role.value.lower()}-{uuid.uuid4().hex[:6]}@bank.example"
        with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
            service = auth_service_factory(session)
            user = service.create_user(email, PASSWORD, "Staff Member", [role], RequestMeta())
            secret: str | None = None
            if mfa:
                secret = service.begin_mfa_enrollment(user.id).secret
                service.confirm_mfa_enrollment(user.id, totp_code(secret, clock), RequestMeta())
                clock.advance(30)  # next TOTP step so the login code is not a replay
        body = login_json(client, email)
        if mfa:
            assert secret is not None
            verify = client.post(
                "/api/v1/auth/mfa/verify",
                json={"mfa_token": body["mfa_token"], "code": totp_code(secret, clock)},
            )
            assert verify.status_code == 200, verify.text
            token = verify.json()["access_token"]
        else:
            token = body["access_token"]
        return Actor(email=email, user_id=user.id, access_token=token, totp_secret=secret)

    return factory


@pytest.fixture
def make_verified_payee(
    client: TestClient, migrated_engine: Engine
) -> Callable[..., dict[str, Any]]:
    """Create + verify a payee for ``owner`` pointing at ``target``'s internal account."""

    def factory(owner: Actor, target: Actor, *, name: str = "Test Customer") -> dict[str, Any]:
        assert target.account_number is not None
        created = client.post(
            "/api/v1/payees",
            headers=owner.headers,
            json={"name": name, "account_number": target.account_number},
        )
        assert created.status_code == 201, created.text
        payee_id = created.json()["id"]
        verified = client.post(f"/api/v1/payees/{payee_id}/verify", headers=owner.headers)
        assert verified.status_code == 200, verified.text
        assert verified.json()["name_match"] == "MATCH"
        return dict(verified.json()["payee"])

    return factory
