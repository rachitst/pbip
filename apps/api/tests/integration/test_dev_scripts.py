"""Local dev scripts keep secrets out of files and logs (CodeQL py/clear-text-storage and
py/clear-text-logging on scripts/seed_dev.py): MFA secrets stay encrypted in the database and
scripts/totp.py derives codes from there. The E2E suite (TASK-077) seeds with a generated
password, takes unused staff codes from totp.py and funds its own customers with e2e_fund.py."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pyotp
import pytest
from pbip_api.core.config import Settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.insights import service as insights_service
from pbip_api.modules.ledger.models import Transaction, TransactionType
from pbip_test_support import PASSWORD, FakeClock, generated_password, totp_code
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from scripts import e2e_fund, seed_dev, totp

pytestmark = pytest.mark.integration


def _cipher(settings: Settings) -> FieldCipher:
    return FieldCipher(settings.field_encryption_key.get_secret_value())


def test_totp_code_is_derived_from_the_encrypted_database_secret(
    db: Session,
    settings: Settings,
    clock: FakeClock,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    service = auth_service_factory(db)
    user = service.create_user(
        "analyst@demo.pbip", PASSWORD, "Analyst", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    secret = service.begin_mfa_enrollment(user.id).secret
    service.confirm_mfa_enrollment(user.id, totp_code(secret, clock), RequestMeta())

    code = totp.current_code(db, _cipher(settings), "  Analyst@Demo.PBIP ", clock.now)

    assert code == totp_code(secret, clock)


def test_totp_returns_none_for_unknown_or_unenrolled_users(
    db: Session, settings: Settings, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    service = auth_service_factory(db)
    user = service.create_user(
        "pending@demo.pbip", PASSWORD, "Pending", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    service.begin_mfa_enrollment(user.id)  # secret stored, enrollment never confirmed

    assert totp.current_code(db, _cipher(settings), "pending@demo.pbip", 0) is None
    assert totp.current_code(db, _cipher(settings), "nobody@demo.pbip", 0) is None


def test_seed_writes_no_secret_file_and_prints_no_secrets(
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(seed_dev, "get_settings", lambda: settings)
    monkeypatch.setattr(seed_dev, "get_engine", lambda: migrated_engine)
    monkeypatch.chdir(tmp_path)  # the old script wrote .dev-secrets.txt into the working directory
    password = generated_password()

    assert seed_dev.main({seed_dev.PASSWORD_ENV: password}) == 0

    output = capsys.readouterr()
    assert list(tmp_path.iterdir()) == []
    assert password not in output.out + output.err
    assert seed_dev.DEMO_PASSWORD not in output.out + output.err
    staff = db.scalars(select(User).where(User.mfa_enabled.is_(True))).all()
    assert {user.email for user in staff} == {email for email, _, _ in seed_dev.STAFF} | set(
        seed_dev.MFA_CUSTOMERS
    )
    for user in staff:
        assert user.mfa_secret_enc is not None
        secret = _cipher(settings).decrypt(user.mfa_secret_enc)
        assert secret not in output.out + output.err
        assert secret not in user.mfa_secret_enc  # stored encrypted, not in plaintext
        # The seeded staff can still log in: totp.py derives the same code from the database.
        assert totp.current_code(db, _cipher(settings), user.email, 1_000_000) == pyotp.TOTP(
            secret
        ).at(1_000_000)


ENV_PASSWORD = generated_password()


def _seed_with(
    settings: Settings, engine: Engine, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> int:
    monkeypatch.setattr(seed_dev, "get_settings", lambda: settings)
    monkeypatch.setattr(seed_dev, "get_engine", lambda: engine)
    return seed_dev.main(environ)


def test_seeded_users_get_the_password_from_the_environment(
    migrated_engine: Engine, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = generated_password()

    assert _seed_with(settings, migrated_engine, monkeypatch, {"PBIP_DEMO_PASSWORD": password}) == 0

    hashes = db.scalars(select(User.password_hash)).all()
    assert len(hashes) == len(seed_dev.CUSTOMERS) + len(seed_dev.STAFF)
    passwords = PasswordService.from_settings(settings)
    assert all(passwords.verify(h, password) for h in hashes)
    assert not any(passwords.verify(h, seed_dev.DEMO_PASSWORD) for h in hashes)


def test_seed_assigns_the_demo_portfolio_once_and_keeps_an_admins_change(
    migrated_engine: Engine, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0032: rm@demo.pbip gets Alice (audited), Bob stays unassigned; a re-run writes nothing
    and does not undo a reassignment an administrator made in between."""
    environ = {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0

    def customer(email: str) -> Customer:
        return db.execute(
            select(Customer).join(User, User.id == Customer.user_id).where(User.email == email)
        ).scalar_one()

    def user_id(email: str) -> UUID:
        return db.execute(select(User.id).where(User.email == email)).scalar_one()

    alice, bob = customer("alice@demo.pbip"), customer("bob@demo.pbip")
    alice_rm = insights_service.assigned_rm(db, alice.id)
    assert alice_rm is not None and alice_rm.rm_email == seed_dev.RM_EMAIL
    assert insights_service.assigned_rm(db, bob.id) is None
    audited = db.execute(
        text("SELECT actor_user_id, resource_id FROM audit_logs WHERE action = 'RM_ASSIGNED'")
    ).all()
    assert [(a.actor_user_id, a.resource_id) for a in audited] == [
        (user_id(seed_dev.ASSIGNING_ADMIN_EMAIL), str(alice.id))
    ]

    other_rm = AuthService(
        db, settings, PasswordService.from_settings(settings), _cipher(settings)
    ).create_user(
        "rm2@bank.example", PASSWORD, "Other RM", [RoleName.RELATIONSHIP_MANAGER], RequestMeta()
    )
    insights_service.assign_rm(db, alice.id, other_rm.id, user_id(seed_dev.ASSIGNING_ADMIN_EMAIL))
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0
    db.expire_all()
    kept = insights_service.assigned_rm(db, alice.id)
    assert kept is not None and kept.rm_user_id == other_rm.id
    assert db.execute(text("SELECT count(*) FROM rm_assignments")).scalar_one() == 2


def test_seed_refuses_without_a_password_outside_local(
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ci_like = settings.model_copy(update={"app_env": "test"})

    assert _seed_with(ci_like, migrated_engine, monkeypatch, {}) == 1

    assert "PBIP_DEMO_PASSWORD" in capsys.readouterr().err
    assert db.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize(
    ("app_env", "environ", "expected"),
    [
        ("local", {}, seed_dev.DEMO_PASSWORD),  # `make seed` on a laptop keeps working
        ("local", {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}, ENV_PASSWORD),
        ("test", {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}, ENV_PASSWORD),
        ("test", {}, None),
        ("test", {"PBIP_DEMO_PASSWORD": ""}, None),
    ],
)
def test_only_local_falls_back_to_the_constant_password(
    app_env: str, environ: dict[str, str], expected: str | None
) -> None:
    assert seed_dev.demo_password(app_env, environ) == expected


@pytest.mark.parametrize(
    ("last_step", "now", "expected"),
    [
        (None, 3000.0, (100, 0.0)),  # never used: the current step
        (98, 3000.0, (100, 0.0)),  # an older step was used: still the current one
        (100, 3010.0, (101, 0.0)),  # current used: the next step is accepted already
        (101, 3010.0, (102, 20.0)),  # current and next used: wait for step 101 to begin
    ],
)
def test_next_unused_step_never_replays_a_used_step(
    last_step: int | None, now: float, expected: tuple[int, float]
) -> None:
    assert totp.next_unused_step(last_step, now) == expected


def test_unused_code_skips_the_step_already_used_and_waits_only_when_it_must(
    db: Session,
    settings: Settings,
    clock: FakeClock,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    service = auth_service_factory(db)
    user = service.create_user(
        "analyst@demo.pbip", PASSWORD, "Analyst", [RoleName.FRAUD_ANALYST], RequestMeta()
    )
    secret = service.begin_mfa_enrollment(user.id).secret
    service.confirm_mfa_enrollment(user.id, totp_code(secret, clock), RequestMeta())
    step = int(clock.now // 30)
    waits: list[float] = []

    code = totp.unused_code(db, _cipher(settings), "analyst@demo.pbip", clock.now, waits.append)
    assert code == pyotp.TOTP(secret).at((step + 1) * 30)  # the enrolment used the current step
    assert waits == []

    db.execute(update(User).where(User.id == user.id).values(mfa_last_step=step + 1))
    code = totp.unused_code(db, _cipher(settings), "analyst@demo.pbip", clock.now, waits.append)
    assert code == pyotp.TOTP(secret).at((step + 2) * 30)
    assert waits == [(step + 1) * 30 - clock.now]


def _customer_account(db: Session, email: str) -> Account:
    customer_id = db.scalar(
        select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == email)
    )
    assert customer_id is not None
    return list_accounts(db, customer_id)[0]


def test_e2e_fund_deposits_through_the_ledger(
    db: Session, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    auth_service_factory(db).register_customer(
        "e2e-fund@example.com", PASSWORD, "Funded Tester", RequestMeta()
    )
    db.commit()

    e2e_fund.fund(db, " E2E-Fund@Example.com ", 12_345_67)

    account = _customer_account(db, "e2e-fund@example.com")
    db.refresh(account)
    assert account.ledger_balance == 12_345_67
    deposit = db.scalars(
        select(Transaction).where(Transaction.credit_account_id == account.id)
    ).one()
    assert deposit.type == TransactionType.DEPOSIT
    assert deposit.reference == e2e_fund.REFERENCE


@pytest.mark.parametrize(
    ("email", "amount", "why"),
    [
        ("alice@demo.pbip", 100_00, "example.com"),  # never a demo or real customer
        ("nobody@example.com", 100_00, "no customer"),
        ("e2e-refused@example.com", 0, "amount"),
        ("e2e-refused@example.com", e2e_fund.MAX_FUND_MINOR + 1, "amount"),
    ],
)
def test_e2e_fund_refuses_anything_but_a_bounded_test_deposit(
    db: Session,
    auth_service_factory: Callable[[Session], AuthService],
    email: str,
    amount: int,
    why: str,
) -> None:
    service = auth_service_factory(db)
    service.register_customer("e2e-refused@example.com", PASSWORD, "Refused", RequestMeta())
    service.register_customer("alice@demo.pbip", PASSWORD, "Alice", RequestMeta())
    db.commit()

    with pytest.raises(e2e_fund.FundingRefusedError, match=why):
        e2e_fund.fund(db, email, amount)

    assert (
        db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.type == TransactionType.DEPOSIT)
        )
        == 0
    )


def test_seed_gives_alice_an_authenticator_and_keeps_the_lab_customers_without(
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """TASK-089 demo readiness: Alice can answer the step-up on a large transfer (totp.py gives her
    code); Bob and Carol stay password-only, because the fraud demo lab refuses a customer with an
    authenticator. An older demo database (seeded before Carol and Alice's authenticator) gets
    both by re-running the seed, which leaves everything else alone and is a no-op the next time."""
    environ = {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}
    monkeypatch.setattr(seed_dev, "MFA_CUSTOMERS", ())
    monkeypatch.setattr(seed_dev, "CUSTOMERS", seed_dev.CUSTOMERS[:2])  # Alice and Bob only
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0
    monkeypatch.undo()
    capsys.readouterr()
    users_before = set(db.scalars(select(User.email)).all())

    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0  # the older database

    assert set(db.scalars(select(User.email)).all()) == users_before | {"carol@demo.pbip"}
    enrolled = set(db.scalars(select(User.email).where(User.mfa_enabled.is_(True))).all())
    assert "alice@demo.pbip" in enrolled
    assert not enrolled & {"bob@demo.pbip", "carol@demo.pbip"}
    assert totp.current_code(db, _cipher(settings), "alice@demo.pbip", 1_000_000) is not None
    first = capsys.readouterr().out
    assert "alice@demo.pbip  (MFA enrolled)" in first and "customer carol@demo.pbip" in first

    alice_secret = db.scalar(select(User.mfa_secret_enc).where(User.email == "alice@demo.pbip"))
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0  # again: no-op
    db.expire_all()
    again = db.scalar(select(User.mfa_secret_enc).where(User.email == "alice@demo.pbip"))
    assert again == alice_secret  # not re-enrolled: the presenter's codes keep working
    assert "MFA enrolled" not in capsys.readouterr().out
