"""Seed a LOCAL database with demo users, funded accounts and MFA-enrolled staff and Alice.

    make seed        (or)    uv run python scripts/seed_dev.py
    docker compose exec -T -e PBIP_DEMO_PASSWORD api python - < scripts/seed_dev.py   (CI E2E job)

Refuses to run against staging/production. Safe to re-run: existing users are skipped, and a demo
customer who already has a relationship manager keeps them (ADR-0032).
Staff MFA secrets live only in the database, encrypted like any other user's; generate a login
code with:  uv run python scripts/totp.py analyst@demo.pbip

Demo customers and what each is for:
  alice@demo.pbip  has an authenticator (``MFA_CUSTOMERS``), so the presenter can show the
                   step-up dialog on a large transfer. Her current code:
                   uv run python scripts/totp.py alice@demo.pbip
  bob@demo.pbip    no authenticator: the payee in Alice's demos and the F6 sender in the fraud lab
  carol@demo.pbip  no authenticator: the fraud demo lab's customer (F1-F5) and mule (F6). The lab
                   signs in with a password only, so it refuses a customer with an authenticator.
Re-running on an existing database adds what is missing (Carol, Alice's authenticator) and leaves
everything else alone, so it is also how an older demo database gets them.

History (ADR-0043: income is a credit source recurring in 2 of the last 3 calendar months;
ADR-0048): each demo customer is credited a monthly salary (``SALARIES``; Alice INR 1,25,000) at
10:00 IST on the 1st of the running month and the two before it, reference "Salary <Month YYYY>".
Alice also gets a month of spending (``ALICE_PAYMENTS``: rent, a home-loan EMI, groceries,
utilities, dining, transport, shopping...) in each of those months up to yesterday, paid to her
seeded, verified external payees (``SEED_PAYEES``), and the verified payee "Bob Demo". Bob and
Carol get the salary only: they are the fraud lab's senders, and a payment history would change
their fraud inputs. Everything is posted in date order through ``ledger.deposit(posted_at=...)``
and ``ledger.seed_payment``, never below zero; a new customer's opening deposit is dated 09:00
IST on the 1st of the oldest month, so the monthly income is exactly the salary. Seeded history
is not fraud-scored (it is history, not a payment being made); later live transfers read it as
the account's history. A line already posted is skipped: a re-run in the same month adds
nothing. The ledger never back-dates before an account's latest entry (every running balance
stays true), so a customer created by an older seed who has since transacted is skipped with a
message: a database seeded from scratch is needed to give them a history.

Usage analytics (ADR-0057): Alice, Bob and Carol turn analytics consent on through the audited
customer service (``ANALYTICS_CUSTOMERS``), so the profile and the staff charts have data. Alice
and Bob get a few weeks of app usage (``CLICKSTREAM``: page views, heartbeats, a loan calculator,
recommendation cards opened) through the events service, sent only while they have no event yet,
and the daily profile job runs once at the end. Carol consents but has no app history: the staff
see a consenting customer without activity. The consent is the demo customers' own decision in a
real bank; here it is seeded so the walkthrough shows the feature, and it can be withdrawn in
Profile like any other.

Neither the demo password nor any MFA secret is printed or written to a file.

The password for every seeded user comes from ``PBIP_DEMO_PASSWORD``. Only APP_ENV=local may fall
back to the ``DEMO_PASSWORD`` constant, so ``make seed`` keeps working on a laptop; any other
environment must pass its own. The CI E2E job always passes a freshly generated one (never printed),
so no password written in this repository guards the stack it tests.
"""

import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

import pyotp
from pbip_api.core.calendar import BANK_TZ
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.crypto import FieldCipher
from pbip_api.core.db import get_engine, utcnow
from pbip_api.core.errors import UnprocessableError
from pbip_api.core.security import PasswordService
from pbip_api.modules.accounts.service import list_accounts
from pbip_api.modules.analytics.profile_job import run_behaviour_profile_job
from pbip_api.modules.auth.models import RoleName, User
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.customers.service import CustomerService
from pbip_api.modules.events import service as events_service
from pbip_api.modules.events.models import ClickstreamEvent
from pbip_api.modules.events.schemas import EventBatchRequest, TelemetryEventIn, TelemetryEventType
from pbip_api.modules.insights import service as insights_service
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import EntryDirection
from pbip_api.modules.payees.models import PayeeStatus
from pbip_api.modules.payees.service import PayeeService
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

DEMO_PASSWORD = "Demo-Password-2026!"  # noqa: S105 - APP_ENV=local fallback only
PASSWORD_ENV = "PBIP_DEMO_PASSWORD"  # noqa: S105  # pragma: allowlist secret - a variable name

CUSTOMERS = [
    ("alice@demo.pbip", "Alice Demo", 25_000_000),  # 2,50,000.00
    ("bob@demo.pbip", "Bob Demo", 5_000_000),
    ("carol@demo.pbip", "Carol Demo", 25_000_000),  # the fraud demo lab's customer
]
# Monthly salary (minor units) credited on the 1st of the running month and the two before it, so
# each demo customer has recurring income (ADR-0043) and the loan and assistant demos have
# something to size an offer from. Alice's INR 1,25,000 makes a 5 lakh car loan affordable.
SALARIES = {
    "alice@demo.pbip": 12_500_000,  # 1,25,000.00
    "bob@demo.pbip": 6_000_000,  # 60,000.00
    "carol@demo.pbip": 9_000_000,  # 90,000.00
}
SALARY_MONTHS = 3
# A new customer's opening deposit at 09:00 IST and each salary at 10:00 IST on the 1st.
OPENING_HOUR, SALARY_HOUR = 9, 10
# Alice's month of spending, repeated for each of the SALARY_MONTHS months up to yesterday:
# (day, hour IST, payee key, reference, amount in minor units). The references use the merchant
# words of analytics/categorization.py, so the breakdown shows housing, groceries, utilities,
# dining, transport, shopping, entertainment and healthcare; the EMI counts as a debt repayment.
# The average payment is about INR 3,000-3,700 (by day of the month), so the fraud demo's amounts
# (demo/demo-script.md) are still 5x or more of it. Only Alice: Bob and Carol are the fraud lab's
# senders, and any payment history would change their AMOUNT_SPIKE and model inputs.
ALICE_PAYMENTS: tuple[tuple[int, int, str, str, int], ...] = (
    (2, 9, "landlord", "Rent {month} - landlord", 1_850_000),
    (3, 9, "lender", "Home loan EMI {month}", 1_250_000),
    (5, 19, "bescom", "BESCOM electricity bill", 238_000),
    (6, 20, "airtel", "Airtel broadband bill", 99_900),
    (7, 18, "bigbasket", "BigBasket groceries", 420_000),
    (8, 8, "chaipoint", "Chai Point coffee", 18_000),
    (10, 21, "netflix", "Netflix subscription", 64_900),
    (11, 22, "spotify", "Spotify subscription", 11_900),
    (12, 21, "swiggy", "Swiggy dinner orders", 164_000),
    (14, 19, "zepto", "Zepto vegetables and dairy", 186_000),
    (15, 9, "ola", "Ola auto rides", 32_000),
    (16, 9, "uber", "Uber rides", 145_000),
    (18, 8, "starbucks", "Starbucks coffee", 85_000),
    (20, 18, "bigbasket", "BigBasket groceries", 340_000),
    (22, 17, "apollo", "Apollo pharmacy", 78_000),
    (24, 21, "zomato", "Zomato dinner", 120_000),
    (26, 20, "amazon", "Amazon electronics", 249_900),
    (28, 19, "zepto", "Zepto groceries", 150_000),
)
SPENDING_CUSTOMERS = ("alice@demo.pbip",)
# The merchants as Alice's verified external payees (payments go out through the clearing
# account like any transfer to another bank). The bank code and numbers are the seed's own.
SEED_PAYEES = {
    "landlord": "R. Sharma (landlord)",
    "lender": "HomeFirst Housing Finance",
    "bescom": "BESCOM",
    "airtel": "Airtel Broadband",
    "bigbasket": "BigBasket",
    "chaipoint": "Chai Point",
    "netflix": "Netflix",
    "spotify": "Spotify",
    "swiggy": "Swiggy",
    "zepto": "Zepto",
    "ola": "Ola",
    "uber": "Uber",
    "starbucks": "Starbucks",
    "apollo": "Apollo Pharmacy",
    "zomato": "Zomato",
    "amazon": "Amazon",
}
SEED_PAYEE_BANK_CODE = "EXTB0002"
SEED_PAYEE_NUMBER_PREFIX = "90000000"  # + 4 digits: 12-digit account numbers
BOB_EMAIL = "bob@demo.pbip"  # Alice's verified payee "Bob Demo" (the step-up demo's payee)
# Customers with an authenticator: step-up on a large transfer to a new payee can be answered,
# instead of every such transfer going to review. Never Bob or Carol (the lab needs them without).
MFA_CUSTOMERS = ("alice@demo.pbip",)
STAFF = [
    ("analyst@demo.pbip", "Fraud Analyst", RoleName.FRAUD_ANALYST),
    ("compliance@demo.pbip", "Compliance Officer", RoleName.COMPLIANCE_OFFICER),
    ("admin@demo.pbip", "Platform Admin", RoleName.ADMIN),
    ("rm@demo.pbip", "Relationship Manager", RoleName.RELATIONSHIP_MANAGER),
]
# ADR-0032: rm@demo.pbip looks after Alice; Bob stays unassigned, so the demo shows an RM's
# portfolio next to the administrator's view of every customer. Assigned by the demo admin.
RM_EMAIL = "rm@demo.pbip"
ASSIGNING_ADMIN_EMAIL = "admin@demo.pbip"
RM_PORTFOLIO = ("alice@demo.pbip",)


# ADR-0057: demo customers who turn usage analytics on, and the app usage seeded for some of them.
ANALYTICS_CUSTOMERS = ("alice@demo.pbip", "bob@demo.pbip", "carol@demo.pbip")
_CREDIT_LINE = "00000000-0000-4000-a000-000000000003"  # pbip_ml catalogue: LOAN
_INDEX_SIP = "00000000-0000-4000-a000-000000000005"  # INVESTMENT
_FIXED_DEPOSIT = "00000000-0000-4000-a000-000000000002"  # SAVINGS
# One visit = (days ago, minutes long, the product card opened or None, loan calculator used).
CLICKSTREAM: dict[str, tuple[tuple[int, int, str | None, bool], ...]] = {
    "alice@demo.pbip": (
        (20, 6, None, False),
        (14, 9, _FIXED_DEPOSIT, False),
        (9, 12, _CREDIT_LINE, True),
        (5, 4, None, True),
        (2, 8, _INDEX_SIP, False),
        (1, 15, _CREDIT_LINE, True),
    ),
    "bob@demo.pbip": ((11, 3, None, False), (3, 5, _INDEX_SIP, False)),
}


def _visit(
    email: str, start: datetime, minutes: int, product: str | None, calculator: bool
) -> list[TelemetryEventIn]:
    """One session: pages at the start, a heartbeat a minute while in use, then the actions."""
    session_id = f"seed-{uuid5(UUID(int=0), f'{email}-{start.isoformat()}').hex[:16]}"
    plan: list[tuple[TelemetryEventType, int, str, dict[str, str | int | float | bool]]] = [
        (TelemetryEventType.PAGE_VIEW, 0, "/accounts", {}),
        (TelemetryEventType.ACCOUNT_VIEWED, 1, "/accounts", {"account_type": "SAVINGS"}),
        (TelemetryEventType.PAGE_VIEW, 2, "/loans" if calculator else "/wealth", {}),
    ]
    if calculator:
        plan.append((TelemetryEventType.LOAN_CALCULATOR_USED, 60, "/loans", {"loan_type": "HOME"}))
    if product is not None:
        plan.append((TelemetryEventType.PRODUCT_CLICKED, 90, "/accounts", {"product_id": product}))
    plan += [
        (TelemetryEventType.SESSION_HEARTBEAT, 60 * m, "/accounts", {}) for m in range(1, minutes)
    ]
    plan.append((TelemetryEventType.PAGE_VIEW, 60 * minutes, "/insights", {}))
    return [
        TelemetryEventIn(
            event_id=uuid5(UUID(int=0), f"{session_id}-{n}"),
            event_type=kind,
            event_timestamp=start + timedelta(seconds=offset),
            session_id=session_id,
            page_url=page,
            properties=props,
        )
        for n, (kind, offset, page, props) in enumerate(plan)
    ]


def _seed_analytics(session: Session, settings: Settings, now: datetime) -> None:
    """Consent through the audited service, a usage history through the events service (only for
    a customer with no event yet, so a re-run adds nothing), then one profile job run."""
    service = CustomerService(session, settings)
    meta = RequestMeta(ip="127.0.0.1", user_agent="seed-script")
    for email in ANALYTICS_CUSTOMERS:
        customer = session.execute(
            select(Customer).join(User, User.id == Customer.user_id).where(User.email == email)
        ).scalar_one_or_none()
        if customer is None:  # not seeded in this database
            continue
        if not customer.analytics_consent:
            service.set_analytics_consent(customer, True, now, meta)
            sys.stdout.write(f"analytics {email}  consent on\n")
        visits = CLICKSTREAM.get(email, ())
        has_events = session.execute(
            select(ClickstreamEvent.id).where(ClickstreamEvent.customer_id == customer.id).limit(1)
        ).first()
        if not visits or has_events is not None:
            continue
        for days_ago, minutes, product, calculator in visits:
            start = (now - timedelta(days=days_ago)).replace(minute=0, second=0, microsecond=0)
            events = _visit(email, start, minutes, product, calculator)
            events_service.ingest_event_batch(
                session, EventBatchRequest(events=events), customer.id, now
            )
        sys.stdout.write(f"analytics {email}  {len(visits)} app session(s)\n")
    result = run_behaviour_profile_job(session, now)
    sys.stdout.write(f"analytics profiles built for {result.profiled} customer(s)\n")


def demo_password(app_env: str, environ: Mapping[str, str]) -> str | None:
    """``PBIP_DEMO_PASSWORD`` if set, the constant only for APP_ENV=local, else None (refuse)."""
    chosen = environ.get(PASSWORD_ENV, "")
    if chosen:
        return chosen
    return DEMO_PASSWORD if app_env == "local" else None


def _user_id(session: Session, email: str) -> UUID:
    return session.execute(select(User.id).where(User.email == email)).scalar_one()


def _enrol_mfa(service: AuthService, user_id: UUID, meta: RequestMeta) -> None:
    """Enrol an authenticator through the normal service, confirmed with the code for "now".

    The clock is pinned for the confirmation only, so the code and the check use one instant
    (a step boundary in between would refuse it); the service's own clock is put back after.
    """
    enrollment = service.begin_mfa_enrollment(user_id)
    now = time.time()
    real_clock = service.clock

    def fixed_clock(saved_now: float = now) -> float:
        return saved_now

    service.clock = fixed_clock
    try:
        service.confirm_mfa_enrollment(user_id, pyotp.TOTP(enrollment.secret).at(int(now)), meta)
    finally:
        service.clock = real_clock


def _enrol_demo_customers(session: Session, service: AuthService, meta: RequestMeta) -> int:
    """Give each of ``MFA_CUSTOMERS`` an authenticator unless they already have one; returns how
    many were enrolled. Only the id and the flag are read: the full row carries the password hash
    and the MFA secret, and nothing read here is printed (CodeQL py/clear-text-logging)."""
    enrolled = 0
    for email in MFA_CUSTOMERS:
        row = session.execute(
            select(User.id, User.mfa_enabled).where(User.email == email)
        ).one_or_none()
        if row is None or row.mfa_enabled:
            continue
        _enrol_mfa(service, row.id, meta)
        enrolled += 1
    return enrolled


def _month_at(now: datetime, months_back: int, hour: int) -> datetime:
    """``hour`` o'clock IST on the 1st of the calendar month ``months_back`` before ``now``'s."""
    local = now.astimezone(BANK_TZ)
    index = local.year * 12 + local.month - 1 - months_back
    return datetime(index // 12, index % 12 + 1, 1, hour, tzinfo=BANK_TZ)


@dataclass(frozen=True, slots=True)
class _Posting:
    """One history line: a salary credit (``payee`` None) or a payment to a seeded payee."""

    at: datetime | None  # None: post now (the running month's salary, see _history_plan)
    reference: str
    amount: int
    payee: str | None = None


def _payee_ids(
    session: Session, payees: PayeeService, customer_id: UUID, internal_bank_code: str
) -> dict[str, UUID]:
    """The customer's seeded payees by key, created and verified through ``PayeeService`` when
    missing. "bob" is Bob Demo's real account (an internal payee, name-checked)."""
    wanted: dict[str, tuple[str, str, str]] = {
        key: (name, SEED_PAYEE_BANK_CODE, f"{SEED_PAYEE_NUMBER_PREFIX}{index:04d}")
        for index, (key, name) in enumerate(SEED_PAYEES.items(), start=1)
    }
    bob = session.execute(
        select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == BOB_EMAIL)
    ).scalar_one_or_none()
    if bob is not None:
        wanted["bob"] = (
            "Bob Demo",
            internal_bank_code,
            list_accounts(session, bob)[0].account_number,
        )
    existing = {(p.bank_code, p.account_number): p for p in payees.list(customer_id)}
    ids: dict[str, UUID] = {}
    for key, (name, bank_code, number) in wanted.items():
        payee = existing.get((bank_code, number))
        if payee is None:
            payee = payees.create(customer_id, name, number, bank_code, None)
        if payee.status == PayeeStatus.PENDING_VERIFICATION:
            payees.verify(payee)
        ids[key] = payee.id
    return ids


def _history(now: datetime, salary: int, payments: bool) -> list[_Posting]:
    """Every line of the seeded history up to the start of today (IST), oldest first.

    Payments stop before today so that nothing seeded counts towards today's spend, the fraud
    velocity windows or the payee-age rules of a demo run later the same day.
    """
    today = now.astimezone(BANK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    lines: list[_Posting] = []
    for months_back in range(SALARY_MONTHS - 1, -1, -1):
        first = _month_at(now, months_back, SALARY_HOUR)
        month = f"{first:%B %Y}"
        lines.append(_Posting(first, f"Salary {month}", salary))
        for day, hour, payee, reference, amount in ALICE_PAYMENTS if payments else ():
            at = first.replace(day=day, hour=hour)
            if at < today:
                lines.append(_Posting(at, reference.format(month=month), amount, payee))
    return sorted(lines, key=lambda line: (line.at or now, line.payee is not None))


def _posted(session: Session, account_id: UUID, line: _Posting) -> bool:
    """Whether the line is already in the ledger: a salary by its reference (its month), a
    payment by its reference at its exact time."""
    assert line.at is not None
    if line.payee is None:
        filters = ledger.EntryFilters(direction=EntryDirection.CREDIT, text=line.reference)
    else:
        filters = ledger.EntryFilters(
            start=line.at,
            end=line.at + timedelta(seconds=1),
            direction=EntryDirection.DEBIT,
            text=line.reference,
        )
    return bool(ledger.list_account_entries(session, account_id, 1, None, filters=filters))


def _latest_entry_at(session: Session, account_id: UUID) -> datetime | None:
    newest = ledger.list_account_entries(session, account_id, 1, None)
    return newest[0][0].created_at if newest else None


def _history_plan(
    session: Session, account_id: UUID, lines: list[_Posting], now: datetime
) -> list[_Posting] | None:
    """The lines still missing, in posting order.

    The ledger only back-dates after an account's latest entry (ADR-0048), so a missing line
    dated earlier makes the history impossible on this database: None, and nothing is posted for
    the customer. The one exception is the running month's salary, which is then credited now
    (last); so is a salary whose 10:00 on the 1st is still ahead.
    """
    latest = _latest_entry_at(session, account_id)
    current_salary = f"Salary {now.astimezone(BANK_TZ):%B %Y}"
    plan: list[_Posting] = []
    late: list[_Posting] = []
    for line in lines:
        if _posted(session, account_id, line):
            continue
        assert line.at is not None
        too_early = latest is not None and line.at < latest
        if line.reference == current_salary and (too_early or line.at > now):
            late.append(_Posting(None, line.reference, line.amount))
        elif too_early:
            return None
        else:
            plan.append(line)
    return plan + late


def _post(
    session: Session, account_id: UUID, user_id: UUID, line: _Posting, payees: dict[str, UUID]
) -> None:
    if line.payee is None:
        ledger.deposit(
            session, account_id, line.amount, reference=line.reference, posted_at=line.at
        )
        return
    assert line.at is not None
    ledger.seed_payment(
        session,
        account_id,
        line.amount,
        reference=line.reference,
        posted_at=line.at,
        payee_id=payees[line.payee],
        initiated_by_user_id=user_id,
    )


def _seed_history(session: Session, internal_bank_code: str, now: datetime) -> None:
    """Post each demo customer's missing history (``SALARIES``; Alice also ``ALICE_PAYMENTS``)
    through the ledger, one database transaction per customer: all of it or, if the ledger
    refuses a line, none of it. A re-run in the same month adds nothing."""
    payee_service = PayeeService(session, get_settings())
    for email, salary in SALARIES.items():
        row = session.execute(
            select(Customer.id, User.id)
            .join(User, User.id == Customer.user_id)
            .where(User.email == email)
        ).one_or_none()
        if row is None:
            continue
        customer_id, user_id = row
        payments = email in SPENDING_CUSTOMERS
        account_id = list_accounts(session, customer_id)[0].id
        plan = _history_plan(session, account_id, _history(now, salary, payments), now)
        if plan is None:
            sys.stdout.write(
                f"history  {email}  skipped: the account has entries after the history's dates;"
                " a history needs a database seeded from scratch\n"
            )
            continue
        payees = (
            _payee_ids(session, payee_service, customer_id, internal_bank_code)
            if payments and plan
            else {}
        )
        try:
            for line in plan:
                _post(session, account_id, user_id, line, payees)
        except (ledger.LedgerInvariantError, UnprocessableError):
            session.rollback()
            sys.stdout.write(f"history  {email}  skipped: the ledger refused a line\n")
            continue
        session.commit()
        if plan:
            sys.stdout.write(f"history  {email}  {len(plan)} posting(s)\n")


def _assign_demo_portfolio(session: Session) -> None:
    """Assign ``RM_PORTFOLIO`` to the demo RM through the audited service; a customer who already
    has an RM (from an earlier run or an admin's change) is left alone."""
    rm_id = _user_id(session, RM_EMAIL)
    admin_id = _user_id(session, ASSIGNING_ADMIN_EMAIL)
    for email in RM_PORTFOLIO:
        customer_id = session.execute(
            select(Customer.id).join(User, User.id == Customer.user_id).where(User.email == email)
        ).scalar_one()
        if insights_service.assigned_rm(session, customer_id) is not None:
            continue
        insights_service.assign_rm(session, customer_id, rm_id, admin_id)
        sys.stdout.write(f"assigned {email}  to {RM_EMAIL}\n")


def main(environ: Mapping[str, str] | None = None) -> int:
    settings = get_settings()
    if settings.is_deployed:
        sys.stderr.write("Refusing to seed a staging/production database.\n")
        return 1
    if not settings.field_encryption_key.get_secret_value():
        sys.stderr.write("FIELD_ENCRYPTION_KEY is empty. Run: cp .env.example .env\n")
        return 1
    password = demo_password(settings.app_env, os.environ if environ is None else environ)
    if password is None:
        sys.stderr.write(
            "Set PBIP_DEMO_PASSWORD: only APP_ENV=local has a default demo password.\n"
        )
        return 1

    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        service = AuthService(
            session,
            settings,
            PasswordService.from_settings(settings),
            FieldCipher(settings.field_encryption_key.get_secret_value()),
        )
        meta = RequestMeta(ip="127.0.0.1", user_agent="seed-script")

        def exists(email: str) -> bool:
            return session.execute(select(User.id).where(User.email == email)).first() is not None

        now = utcnow()
        opened = _month_at(now, SALARY_MONTHS - 1, OPENING_HOUR)  # before the first salary
        for email, name, funds in CUSTOMERS:
            if exists(email):
                continue
            user = service.register_customer(email, password, name, meta)
            customer = session.execute(
                select(Customer).where(Customer.user_id == user.id)
            ).scalar_one()
            account = list_accounts(session, customer.id)[0]
            ledger.deposit(
                session, account.id, funds, reference="Demo opening balance", posted_at=opened
            )
            session.commit()
            sys.stdout.write(f"customer {email}  account {account.account_number}\n")

        for email, name, role in STAFF:
            if exists(email):
                continue
            user = service.create_user(email, password, name, [role], meta)
            _enrol_mfa(service, user.id, meta)
            sys.stdout.write(f"staff    {email}  ({role.value}, MFA enrolled)\n")

        if _enrol_demo_customers(session, service, meta):
            sys.stdout.write("demo customers in MFA_CUSTOMERS: authenticator enrolled\n")
        _assign_demo_portfolio(session)
        _seed_history(session, settings.internal_bank_code, now)
        _seed_analytics(session, settings, now)

    sys.stdout.write(
        "\nDemo password (all new users): PBIP_DEMO_PASSWORD if set, else (APP_ENV=local only)\n"
        "the DEMO_PASSWORD constant in scripts/seed_dev.py.\n"
        "MFA code (staff and alice@demo.pbip): uv run python scripts/totp.py <email>\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
