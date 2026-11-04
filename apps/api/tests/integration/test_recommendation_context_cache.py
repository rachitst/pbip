"""The version-checked recommendation context cache (#343, ADR-0054), against real PostgreSQL.

Verifies:
- a warm request reads only the accounts (with their row versions), the segment and nothing else,
  and gives exactly the context a cold build gives;
- every write that changes the inputs is seen by the next request: a deposit, a payment out, money
  received from ANOTHER customer, a new account, a fixed deposit booked and closed, a new day;
- consent and the date of birth are read live: withdrawing consent shows on the very next request
  even though the cached part is reused (a cache hit, proven by the statement count);
- an entry is only ever used for the customer it was built for.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.accounts.policy import list_owned_accounts_with_versions
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import TransactionType
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations import CustomerRecommendationContext, Recommendation
from pbip_ml.recommendations.propensity import PropensityModel
from pbip_test_support import Actor, age_payee, count_statements
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

pytestmark = [pytest.mark.integration, pytest.mark.security]

RECS = "/api/v1/recommendations"
FD_REGULAR = "20000000-0000-4000-a000-000000000002"


@pytest.fixture(autouse=True)
def cold_cache() -> Iterator[None]:
    recommendations_service.clear_context_cache()
    yield
    recommendations_service.clear_context_cache()


@pytest.fixture
def engine_inputs(monkeypatch: pytest.MonkeyPatch) -> list[CustomerRecommendationContext]:
    """Record exactly what the recommendation engine is given, then run it for real."""
    seen: list[CustomerRecommendationContext] = []
    real = recommendations_service.generate_recommendations

    def spy(
        ctx: CustomerRecommendationContext,
        top_k: int = 3,
        propensity: PropensityModel | None = None,
        apply_risk_band: bool = True,
    ) -> list[Recommendation]:
        seen.append(ctx)
        return real(ctx, top_k=top_k, propensity=propensity, apply_risk_band=apply_risk_band)

    monkeypatch.setattr(recommendations_service, "generate_recommendations", spy)
    return seen


def _fresh(engine: Engine, actor: Actor) -> CustomerRecommendationContext:
    """The context built with no cache at all, from a separate session."""
    assert actor.customer_id is not None
    recommendations_service.clear_context_cache()
    with sessionmaker(bind=engine)() as session:
        ctx = recommendations_service._context(session, actor.customer_id, utcnow())
    assert ctx is not None
    return ctx


def _served(
    client: TestClient, actor: Actor, engine: Engine, inputs: list[CustomerRecommendationContext]
) -> tuple[CustomerRecommendationContext, list[str]]:
    """What the next request gives the engine, and the statements it sent."""
    with count_statements(engine) as sent:
        assert client.get(RECS, headers=actor.headers).status_code == 200
    return inputs[-1], sent


def _rebuilt(sent: list[str]) -> bool:
    """The request read the ledger entries again (the cold build), not the cached figures."""
    return any("FROM transaction_entries" in sql for sql in sent)


def test_a_warm_request_reads_accounts_and_segment_only_and_equals_a_cold_build(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    """Cold: token check, customer, accounts, entries, segment, loans, deposits, lockouts (8).
    Warm: token check, customer, accounts, segment, lockouts (5). The lockout count (UC-06 loan
    rule 1, ADR-0056) changes without touching an account, so it is read live. The engine gets
    the same context either way."""
    alice = make_customer(salary=150_000_00)
    cold, cold_sent = _served(client, alice, migrated_engine, engine_inputs)
    warm, sent = _served(client, alice, migrated_engine, engine_inputs)
    assert (len(cold_sent), len(sent)) == (8, 5), sent
    assert _rebuilt(cold_sent) and not _rebuilt(sent)
    assert warm == cold == _fresh(migrated_engine, alice)


def test_every_write_to_the_inputs_is_seen_by_the_next_request(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    alice = make_customer(salary=150_000_00)
    bob = make_customer(funds=500_000_00)
    assert alice.account_id is not None and bob.account_id is not None
    seen = [_served(client, alice, migrated_engine, engine_inputs)[0]]

    def changed() -> None:
        ctx, sent = _served(client, alice, migrated_engine, engine_inputs)
        assert _rebuilt(sent), "a write to the inputs must make the next request rebuild them"
        assert ctx == _fresh(migrated_engine, alice)
        assert ctx != seen[-1]
        seen.append(ctx)

    with sessionmaker(bind=migrated_engine)() as session:  # a deposit
        ledger.deposit(session, alice.account_id, 40_000_00, reference="Bonus")
        session.commit()
    changed()

    payee = make_verified_payee(bob, alice)  # money in from ANOTHER customer's transfer
    age_payee(migrated_engine, payee["id"], 48)
    received = client.post(
        "/api/v1/transfers",
        headers={**bob.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "from_account_id": str(bob.account_id),
            "amount_minor": 5_000_00,
            "payee_id": payee["id"],
        },
    )
    assert received.status_code == 201, received.text
    changed()

    booked = client.post(  # a fixed deposit: a new held product, paid from the account
        "/api/v1/wealth/fixed-deposits/book",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
        json={
            "product_id": FD_REGULAR,
            "amount_minor": 100_000_00,
            "tenure_months": 12,
            "account_id": str(alice.account_id),
        },
    )
    assert booked.status_code == 201, booked.text
    changed()
    assert [h.kind.value for h in seen[-1].held_products] == ["SAVINGS_ACCOUNT", "FIXED_DEPOSIT"]

    closed = client.post(
        f"/api/v1/wealth/fixed-deposits/{booked.json()['id']}/close",
        headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert closed.status_code == 200, closed.text
    changed()
    assert [h.kind.value for h in seen[-1].held_products] == ["SAVINGS_ACCOUNT"]

    opened = client.post(
        "/api/v1/accounts", headers=alice.headers, json={"account_type": "CURRENT"}
    )
    assert opened.status_code == 201, opened.text  # a new account and nothing else
    changed()
    assert [h.kind.value for h in seen[-1].held_products] == ["SAVINGS_ACCOUNT", "CURRENT_ACCOUNT"]


def test_withdrawn_consent_shows_on_the_next_request_while_the_cached_part_is_reused(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    """Consent and the date of birth never go into the cache (ADR-0015): they are read from the
    customer row that authentication loads on every request."""
    alice = make_customer(funds=200_000_00)
    consent = "/api/v1/customers/me/recommendations-consent"
    client.put(
        "/api/v1/customers/me/date-of-birth",
        headers=alice.headers,
        json={"date_of_birth": "1950-01-01"},
    )
    client.put(consent, headers=alice.headers, json={"granted": True})
    granted, _cold = _served(client, alice, migrated_engine, engine_inputs)
    assert granted.age_band == "60+"

    assert client.put(consent, headers=alice.headers, json={"granted": False}).status_code == 200
    withdrawn, sent = _served(client, alice, migrated_engine, engine_inputs)
    assert len(sent) == 5 and not _rebuilt(sent)  # a cache hit ...
    assert withdrawn.age_band == "unknown"  # ... that still sees the withdrawal at once
    assert withdrawn.model_copy(update={"age_band": "60+"}) == granted

    client.put(consent, headers=alice.headers, json={"granted": True})
    client.delete("/api/v1/customers/me/date-of-birth", headers=alice.headers)
    deleted, sent = _served(client, alice, migrated_engine, engine_inputs)
    assert (len(sent), _rebuilt(sent), deleted.age_band) == (5, False, "unknown")


def test_a_new_day_rebuilds_the_inputs(
    make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """Health and cash flow are per calendar month and deposit maturity per bank day, so the
    date is part of the version."""
    alice = make_customer(salary=150_000_00)
    assert alice.customer_id is not None
    now = utcnow()
    with sessionmaker(bind=migrated_engine)() as db:
        recommendations_service.get_customer_recommendations(db, alice.customer_id, now)
        with count_statements(migrated_engine) as same_day:
            recommendations_service.get_customer_recommendations(db, alice.customer_id, now)
        with count_statements(migrated_engine) as next_day:
            recommendations_service.get_customer_recommendations(
                db, alice.customer_id, now + timedelta(days=1)
            )
    assert not _rebuilt(same_day)
    assert _rebuilt(next_day)


def test_an_entry_is_only_used_for_the_customer_it_was_built_for(
    client: TestClient,
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    engine_inputs: list[CustomerRecommendationContext],
) -> None:
    rich = make_customer(salary=300_000_00)
    poor = make_customer()
    rich_ctx, _ = _served(client, rich, migrated_engine, engine_inputs)
    poor_ctx, sent = _served(client, poor, migrated_engine, engine_inputs)
    assert _rebuilt(sent)  # built for this customer, not taken from the other one's entry
    assert poor_ctx.customer_id == poor.customer_id != rich_ctx.customer_id
    assert (poor_ctx.total_balance_minor, poor_ctx.monthly_inflow_minor) == (0, 0)
    assert rich_ctx.monthly_inflow_minor > 0
    again, _ = _served(client, rich, migrated_engine, engine_inputs)
    assert again == rich_ctx


def test_the_row_version_changes_with_every_write_and_not_with_a_read_or_a_lock(
    make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    """What the cache relies on (PostgreSQL ``xmin``): a posting and a hold each write a new row
    version; reading the account or locking it ``FOR UPDATE`` does not."""
    alice = make_customer(funds=100_000_00)
    bob = make_customer()
    customer_id, account_id = alice.customer_id, alice.account_id
    assert customer_id is not None and account_id is not None and bob.account_id is not None
    factory = sessionmaker(bind=migrated_engine)

    def version() -> int:
        with factory() as session:
            [(_account, row_version)] = list_owned_accounts_with_versions(session, customer_id)
            return row_version

    first = version()
    assert version() == first
    with factory() as session:
        ledger.lock_accounts(session, [account_id])  # what every posting takes first
        session.commit()
    assert version() == first
    with factory() as session:
        ledger.deposit(session, account_id, 1_00, reference="Interest")
        session.commit()
    after_deposit = version()
    assert after_deposit != first
    with factory() as session:  # a transfer held for review reserves the amount
        held = ledger.create_transaction(
            session,
            type_=TransactionType.TRANSFER,
            amount=5_00,
            currency="INR",
            debit_account_id=account_id,
            credit_account_id=bob.account_id,
        )
        ledger.place_hold(session, held)
        session.commit()
    assert version() not in (first, after_deposit)
