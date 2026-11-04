"""Local dev scripts keep secrets out of files and logs (CodeQL py/clear-text-storage and
py/clear-text-logging on scripts/seed_dev.py): MFA secrets stay encrypted in the database and
scripts/totp.py derives codes from there. The E2E suite (TASK-077) seeds with a generated
password, takes unused staff codes from totp.py and funds its own customers with e2e_fund.py."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pyotp
import pytest
from pbip_api.core.calendar import BANK_TZ
from pbip_api.core.config import Settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import utcnow
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.analytics import repository as analytics_repository
from pbip_api.modules.analytics import service as analytics_service
from pbip_api.modules.assistant import fallback, tools
from pbip_api.modules.assistant.faq import FaqFacts
from pbip_api.modules.assistant.tools import ToolContext, ToolName
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.insights import service as insights_service
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import (
    EntryDirection,
    Transaction,
    TransactionEntry,
    TransactionType,
)
from pbip_api.modules.loans import service as loans_service
from pbip_api.modules.loans.catalogue import PRODUCTS as LOAN_PRODUCTS
from pbip_api.modules.payees.models import PayeeStatus
from pbip_api.modules.payees.service import PayeeService
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


def test_e2e_fund_with_a_salary_gives_regular_income_for_the_loan_journey(
    db: Session, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    """Two back-dated salaries (1st of the two previous months, 10:00 IST) then the opening
    balance: ADR-0043 sees regular income, so the loan journey can apply (TASK-077)."""
    auth_service_factory(db).register_customer(
        "e2e-salary@example.com", PASSWORD, "Salaried Tester", RequestMeta()
    )
    db.commit()

    e2e_fund.fund(db, "e2e-salary@example.com", 50_000_00, salary_minor=80_000_00)

    account = _customer_account(db, "e2e-salary@example.com")
    db.refresh(account)
    assert account.ledger_balance == 50_000_00 + 2 * 80_000_00
    deposits = db.scalars(
        select(Transaction)
        .where(Transaction.credit_account_id == account.id)
        .order_by(Transaction.created_at)
    ).all()
    salaries, opening = deposits[:-1], deposits[-1]
    assert opening.reference == e2e_fund.REFERENCE
    expected = e2e_fund.salary_dates(utcnow())
    assert [t.posted_at for t in salaries] == expected
    assert [t.reference for t in salaries] == [f"Salary {at:%B %Y}" for at in expected]
    assert all(at.astimezone(BANK_TZ).day == 1 and at < utcnow() for at in expected)
    user_id, customer_id = db.execute(
        select(User.id, Customer.id)
        .join(Customer, Customer.user_id == User.id)
        .where(User.email == "e2e-salary@example.com")
    ).one()
    product = LOAN_PRODUCTS[0]
    check = loans_service.check_eligibility(
        db,
        customer_id=customer_id,
        user_id=user_id,
        product_id=product.id,
        amount_minor=product.min_amount_minor,
        tenure_months=product.tenures_months[0],
    )
    assert check.status == "ELIGIBLE" and check.monthly_income_minor > 0


def test_e2e_fund_refuses_an_unbounded_salary(
    db: Session, auth_service_factory: Callable[[Session], AuthService]
) -> None:
    auth_service_factory(db).register_customer(
        "e2e-greedy@example.com", PASSWORD, "Greedy", RequestMeta()
    )
    db.commit()
    with pytest.raises(e2e_fund.FundingRefusedError, match="salary"):
        e2e_fund.fund(db, "e2e-greedy@example.com", 100_00, e2e_fund.MAX_FUND_MINOR + 1)
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["x", "a@example.com", "100", "--salary"], 2),
        (["x", "a@example.com", "100", "--salary", "-5"], 2),
        (["x", "a@example.com", "100", "--wage", "5"], 2),
        (["x", "a@example.com"], 2),
    ],
)
def test_e2e_fund_usage_errors(argv: list[str], code: int) -> None:
    assert e2e_fund.main(argv) == code


def test_salary_dates_are_the_two_previous_months_whatever_the_day() -> None:
    first_morning = datetime(2026, 1, 1, 0, 30, tzinfo=BANK_TZ)  # before 10:00 on a 1st
    assert e2e_fund.salary_dates(first_morning) == [
        datetime(2025, 11, 1, 10, tzinfo=BANK_TZ),
        datetime(2025, 12, 1, 10, tzinfo=BANK_TZ),
    ]
    assert e2e_fund.salary_dates(datetime(2026, 3, 31, 23, tzinfo=BANK_TZ))[0].month == 1


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
    assert "MFA_CUSTOMERS: authenticator enrolled" in first and "customer carol@demo.pbip" in first

    alice_secret = db.scalar(select(User.mfa_secret_enc).where(User.email == "alice@demo.pbip"))
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0  # again: no-op
    db.expire_all()
    again = db.scalar(select(User.mfa_secret_enc).where(User.email == "alice@demo.pbip"))
    assert again == alice_secret  # not re-enrolled: the presenter's codes keep working
    assert "authenticator enrolled" not in capsys.readouterr().out


def _customer(db: Session, email: str) -> tuple[UUID, UUID, UUID]:
    """(customer id, user id, primary account id) of a seeded customer."""
    customer_id, user_id = db.execute(
        select(Customer.id, User.id)
        .join(User, User.id == Customer.user_id)
        .where(User.email == email)
    ).one()
    return customer_id, user_id, list_accounts(db, customer_id)[0].id


def _salary_credits(db: Session, account_id: UUID) -> list[Transaction]:
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.credit_account_id == account_id,
                Transaction.reference.like("Salary %"),
            )
            .order_by(Transaction.created_at)
        ).all()
    )


def test_seed_gives_each_demo_customer_a_salary_history_that_is_income(
    migrated_engine: Engine, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0043 counts a credit as income only when its source recurs in 2 of the last 3 calendar
    months, so a single opening deposit left the demo customers with no income and no loan offer.
    The seed credits a monthly salary through ``ledger.deposit(posted_at=...)`` (ADR-0048)."""
    environ = {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0
    now = utcnow()

    for email, salary in seed_dev.SALARIES.items():
        customer_id, _, account_id = _customer(db, email)
        credits = _salary_credits(db, account_id)
        months = {
            (c.created_at.astimezone(BANK_TZ).year, c.created_at.astimezone(BANK_TZ).month)
            for c in credits
        }
        assert len(credits) == seed_dev.SALARY_MONTHS == len(months), email
        assert all(
            c.type == TransactionType.DEPOSIT
            and c.debit_account_id == ledger.SYSTEM_CLEARING_ID
            and c.amount == salary
            and c.reference == f"Salary {c.created_at.astimezone(BANK_TZ):%B %Y}"
            for c in credits
        )
        profile = analytics_service.get_cash_flow_profile(db, customer_id, account_id, now)
        assert profile.monthly_income_minor == salary, email  # exactly the salary, every month
        account = db.get(Account, account_id)
        assert account is not None
        assert account.ledger_balance == ledger.entry_balance(db, account_id)
        # In time order the running balances are true (statements and forecasts read them so).
        balances = db.execute(
            select(
                TransactionEntry.direction, TransactionEntry.amount, TransactionEntry.balance_after
            )
            .where(TransactionEntry.account_id == account_id)
            .order_by(TransactionEntry.created_at, TransactionEntry.seq)
        ).all()
        running = 0
        for direction, amount, balance_after in balances:
            running += int(amount) if direction == EntryDirection.CREDIT else -int(amount)
            assert int(balance_after) == running, email

    # The demo question gets an eligible answer, through the assistant's own routing and tool.
    customer_id, user_id, _ = _customer(db, "alice@demo.pbip")
    question = fallback.route("can I afford a 5 lakh car loan over 5 years?")
    assert question.tool == ToolName.LOAN_AFFORDABILITY
    ctx = ToolContext(db, customer_id, user_id, now, FaqFacts.from_settings(settings))
    reply = tools.loan_affordability(ctx, question.arguments).text
    assert reply.startswith("For a car loan of INR 5,00,000.00 over 60 months you are likely to")
    assert "of your income of INR 1,25,000.00, within the 50% limit" in reply

    entries = db.scalar(select(func.count()).select_from(TransactionEntry))
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0  # again: no-op
    assert db.scalar(select(func.count()).select_from(TransactionEntry)) == entries


def test_seed_gives_alice_a_spending_history_in_date_order_with_the_expected_categories(
    migrated_engine: Engine, db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assistant's spending, forecast and recent-transactions answers need history. Alice's is
    posted through ``ledger.seed_payment`` to her seeded payees, in date order, never below zero,
    categorised as the merchant words say, unscored, with an average payment that keeps the fraud
    demo's amounts (demo/demo-script.md) at 5x or more."""
    environ = {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0
    now = utcnow()
    customer_id, user_id, account_id = _customer(db, "alice@demo.pbip")

    rows = db.execute(
        select(TransactionEntry.created_at, TransactionEntry.balance_after)
        .where(TransactionEntry.account_id == account_id)
        .order_by(TransactionEntry.seq)
    ).all()
    stamps = [r.created_at for r in rows]
    assert stamps == sorted(stamps)  # posting order is date order
    assert all(int(r.balance_after) >= 0 for r in rows)
    today = now.astimezone(BANK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    payments = list(
        db.scalars(select(Transaction).where(Transaction.debit_account_id == account_id)).all()
    )
    assert payments and all(
        p.type == TransactionType.TRANSFER
        and p.credit_account_id == ledger.SYSTEM_CLEARING_ID
        and p.payee_id is not None
        and p.initiated_by_user_id == user_id
        and p.created_at < today
        for p in payments
    )
    scored = db.scalar(
        select(func.count())
        .select_from(FraudEvent)
        .where(FraudEvent.transaction_id.in_([p.id for p in payments]))
    )
    assert scored == 0  # history is not a payment being made: never fraud-scored

    # Last calendar month (closed, complete): every merchant line in its category.
    local = now.astimezone(BANK_TZ)
    year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
    start = datetime(year, month, 1, tzinfo=BANK_TZ)
    end = datetime(local.year, local.month, 1, tzinfo=BANK_TZ)
    by_category: dict[str, int] = {}
    for debit in analytics_repository.spend_debits(db, account_id, {account_id}, start, end):
        by_category[debit.category] = by_category.get(debit.category, 0) + debit.amount_minor
    assert by_category == {
        "housing": 1_850_000,
        "transfers": 1_250_000,  # the home-loan EMI (a debt repayment, no merchant category)
        "groceries": 420_000 + 186_000 + 340_000 + 150_000,
        "utilities": 238_000 + 99_900,
        "dining": 18_000 + 164_000 + 85_000 + 120_000,
        "entertainment": 64_900 + 11_900,
        "transport": 32_000 + 145_000,
        "healthcare": 78_000,
        "shopping": 249_900,
    }

    # The fraud baseline's 90-day average transfer: the demo's INR 35,000 is still a spike.
    average = db.scalar(
        select(func.avg(Transaction.amount)).where(
            Transaction.debit_account_id == account_id,
            Transaction.type == TransactionType.TRANSFER,
            Transaction.created_at >= now - timedelta(days=90),
        )
    )
    assert average is not None and 2_500_00 <= float(average) <= 4_000_00
    assert 5 * float(average) <= 35_000_00

    payees = {p.name: p for p in PayeeService(db, settings).list(customer_id)}
    assert set(payees) == set(seed_dev.SEED_PAYEES.values()) | {"Bob Demo"}
    assert all(p.status == PayeeStatus.VERIFIED for p in payees.values())
    for email in ("bob@demo.pbip", "carol@demo.pbip"):  # the fraud lab's senders: salary only
        _, _, other = _customer(db, email)
        assert db.scalar(select(func.count()).where(Transaction.debit_account_id == other)) == 0

    entries = db.scalar(select(func.count()).select_from(TransactionEntry))
    payee_count = len(payees)
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0  # again: no-op
    assert db.scalar(select(func.count()).select_from(TransactionEntry)) == entries
    assert len(PayeeService(db, settings).list(customer_id)) == payee_count


def test_seed_skips_the_salary_history_of_a_customer_who_transacted_since(
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An older demo database: Alice transacted after the salary dates. The ledger never back-dates
    before an account's latest entry, so the seed posts nothing for her and says why; Bob, whose
    only entry is his opening deposit, still gets his history."""
    environ = {"PBIP_DEMO_PASSWORD": ENV_PASSWORD}
    monkeypatch.setattr(seed_dev, "SALARIES", {})  # an older seed: no salaries
    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0
    monkeypatch.undo()
    _, _, alice_account = _customer(db, "alice@demo.pbip")
    ledger.deposit(db, alice_account, 1_000, reference="Cash")  # now: after every salary date
    db.commit()
    capsys.readouterr()

    assert _seed_with(settings, migrated_engine, monkeypatch, environ) == 0

    out = capsys.readouterr().out
    assert "history  alice@demo.pbip  skipped" in out
    assert _salary_credits(db, alice_account) == []
    _, _, bob_account = _customer(db, "bob@demo.pbip")
    assert len(_salary_credits(db, bob_account)) == seed_dev.SALARY_MONTHS


def test_the_seed_writes_money_only_through_the_ledger_service() -> None:
    """AGENTS.md section 4: no entry, balance or trigger is touched outside ledger/service.py."""
    source = (Path(seed_dev.__file__)).read_text(encoding="utf-8")
    for forbidden in (
        "TransactionEntry",
        "ledger_balance",
        "held_amount",
        "TRIGGER",
        "INSERT",
        "UPDATE ",
        "_redate",
        "text(",
    ):
        assert forbidden not in source, forbidden
    assert "ledger.deposit(" in source and "ledger.seed_payment(" in source
