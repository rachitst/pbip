"""Issue #314 / ADR-0036: unanswered step-ups expire, idempotency keys expire and are purged."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.ledger.key_purge import purge_expired_keys
from pbip_api.modules.ledger.models import IdempotencyKey, Transaction, TransactionStatus
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.payments import scheduled_service
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
)
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    execute_scheduled_instruction,
)
from pbip_api.modules.payments.step_up_expiry import expire_stale_step_ups
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, FakeClock, age_payee, totp_code
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

MSEB_BILLER_ID = "00000000-0000-0000-0000-0000000000b1"
TIMEOUT = timedelta(minutes=15)  # the default STEP_UP_EXPIRY_MINUTES


def new_key() -> dict[str, str]:
    return {"Idempotency-Key": f"key-{uuid.uuid4().hex}"}


@pytest.fixture
def pair(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice, bob = make_customer(funds=100_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, bob, payee


def send(
    client: TestClient,
    alice: Actor,
    payee: dict[str, Any],
    amount: int,
    key: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **(key or new_key())},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
        },
    )


def balance(client: TestClient, actor: Actor) -> dict[str, int]:
    body = client.get(f"/api/v1/accounts/{actor.account_id}", headers=actor.headers).json()
    return {"ledger": body["ledger_balance_minor"], "held": body["held_minor"]}


def force(app: FastAPI, decision: FraudDecision) -> None:
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(decision)


def enable_mfa(
    auth_service_factory: Callable[[Session], AuthService],
    engine: Engine,
    actor: Actor,
    clock: FakeClock,
) -> str:
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        service = auth_service_factory(session)
        secret = service.begin_mfa_enrollment(actor.user_id).secret
        service.confirm_mfa_enrollment(actor.user_id, totp_code(secret, clock), RequestMeta())
    clock.advance(30)
    return secret


def step_up(client: TestClient, actor: Actor, tid: str, code: str) -> Any:
    return client.post(
        f"/api/v1/transfers/{tid}/step-up",
        headers={**actor.headers, **new_key()},
        json={"code": code},
    )


def status_of(engine: Engine, tid: str) -> tuple[str, str | None]:
    with Session(engine) as session:
        row = session.get(Transaction, uuid.UUID(tid))
        assert row is not None
        return row.status.value, row.decline_reason


@pytest.fixture
def waiting(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> tuple[Actor, Actor, dict[str, Any], str, str]:
    """Alice (MFA on) with one 50,000 transfer waiting for her code."""
    alice, bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force(app, FraudDecision.STEP_UP)
    response = send(client, alice, payee, 50_000)
    assert response.status_code == 202 and response.json()["step_up_required"] is True
    return alice, bob, payee, response.json()["id"], secret


# ------------------------------------------------------------------- step-up expiry
def test_an_unanswered_step_up_is_declined_after_the_timeout_audited_and_notified(
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    settings: Settings,
    clock: FakeClock,
) -> None:
    alice, bob, _payee, tid, secret = waiting
    before = balance(client, alice)
    early = expire_stale_step_ups(migrated_engine, now=utcnow(), settings=settings)
    assert early["EXPIRED"] == 0 and status_of(migrated_engine, tid)[0] == "PENDING_STEP_UP"

    later = utcnow() + TIMEOUT + timedelta(seconds=1)
    assert expire_stale_step_ups(migrated_engine, now=later, settings=settings)["EXPIRED"] == 1
    assert status_of(migrated_engine, tid) == ("DECLINED", "STEP_UP_EXPIRED")
    # Nothing was held or posted while it waited, so nothing moves now.
    assert balance(client, alice) == before and balance(client, bob)["ledger"] == 0
    with Session(migrated_engine) as session:
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_STEP_UP_EXPIRED")
        ).scalar_one()
        assert audit.resource_id == tid and audit.actor_user_id is None
        assert audit.details["actor"] == "scheduler" and audit.details["timeout_minutes"] == 15
        declined = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "TRANSFER_DECLINED")
        ).all()
        assert [e.payload["transaction_id"] for e in declined] == [tid]
    # The customer sees the plain declined message, never a reason.
    read = client.get(f"/api/v1/transfers/{tid}", headers=alice.headers).json()
    assert read["status"] == "DECLINED" and "STEP_UP" not in str(read)
    # Idempotent: a re-delivered run finds nothing; a late code is refused.
    assert expire_stale_step_ups(migrated_engine, now=later, settings=settings)["EXPIRED"] == 0
    assert step_up(client, alice, tid, totp_code(secret, clock)).status_code == 409
    assert balance(client, bob)["ledger"] == 0


def test_a_step_up_past_the_timeout_cannot_be_completed_before_the_job_runs(
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, bob, _payee, tid, secret = waiting
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE transactions SET created_at = now() - interval '16 minutes' WHERE id = :t"
            ),
            {"t": tid},
        )
    refused = step_up(client, alice, tid, totp_code(secret, clock))
    assert refused.status_code == 409
    assert status_of(migrated_engine, tid)[0] == "PENDING_STEP_UP"  # the job declines it
    assert balance(client, bob)["ledger"] == 0


def test_answered_and_escalated_step_ups_are_never_expired(
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    settings: Settings,
    clock: FakeClock,
) -> None:
    alice, bob, payee, tid, secret = waiting
    assert step_up(client, alice, tid, totp_code(secret, clock)).status_code == 200
    escalated = send(client, alice, payee, 30_000).json()["id"]
    for _ in range(3):
        step_up(client, alice, escalated, "000000")
    assert status_of(migrated_engine, escalated)[0] == "PENDING_REVIEW"
    later = utcnow() + TIMEOUT * 4
    assert expire_stale_step_ups(migrated_engine, now=later, settings=settings)["EXPIRED"] == 0
    assert status_of(migrated_engine, tid)[0] == "POSTED"
    assert status_of(migrated_engine, escalated)[0] == "PENDING_REVIEW"
    assert balance(client, bob)["ledger"] == 50_000 and balance(client, alice)["held"] == 30_000


def test_an_expired_step_up_stops_counting_against_the_daily_limit(
    app: FastAPI,
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    """Default daily limit 50,000,000: two 20,000,000 step-ups left waiting block a third."""
    alice, _bob, payee, _tid, _secret = waiting
    send(client, alice, payee, 20_000_000)
    send(client, alice, payee, 20_000_000)
    force(app, FraudDecision.ALLOW)
    assert send(client, alice, payee, 20_000_000).json()["code"] == "limit_exceeded"
    later = utcnow() + TIMEOUT + timedelta(seconds=1)
    assert expire_stale_step_ups(migrated_engine, now=later, settings=settings)["EXPIRED"] == 3
    assert send(client, alice, payee, 20_000_000).status_code == 201


def test_step_up_completion_racing_the_expiry_job_settles_exactly_once(
    app: FastAPI,
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    settings: Settings,
    clock: FakeClock,
) -> None:
    """Real threads on PostgreSQL. The job sees the transfer as overdue, the customer's request
    does not (its deadline is the wall clock): both take the customer's advisory lock first, so
    exactly one wins and the other sees its result. Repeated so both orders get a chance."""
    alice, bob, payee, first, secret = waiting
    credited = 0
    for round_ in range(4):
        tid = first if round_ == 0 else send(client, alice, payee, 50_000).json()["id"]
        code = totp_code(secret, clock)
        later = utcnow() + TIMEOUT + timedelta(minutes=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(step_up, TestClient(app), alice, tid, code)
            job = pool.submit(expire_stale_step_ups, migrated_engine, now=later, settings=settings)
            response, counts = confirm.result(), job.result()
        status, reason = status_of(migrated_engine, tid)
        if status == "POSTED":
            assert response.status_code == 200 and counts["EXPIRED"] == 0
            credited += 50_000
        else:
            assert (status, reason) == ("DECLINED", "STEP_UP_EXPIRED")
            assert response.status_code == 409 and counts["EXPIRED"] == 1
        assert balance(client, bob)["ledger"] == credited
        assert balance(client, alice) == {"ledger": 100_000_000 - credited, "held": 0}
        clock.advance(30)


# --------------------------------------------------------------- idempotency key TTL
def expire_keys(engine: Engine, *keys: dict[str, str]) -> None:
    with engine.begin() as connection:
        for key in keys:
            connection.execute(
                text(
                    "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
                    "WHERE key = :k"
                ),
                {"k": key["Idempotency-Key"]},
            )


def test_an_expired_key_counts_as_unused(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, bob, payee = pair
    key = new_key()
    first = send(client, alice, payee, 40_000, key)
    assert send(client, alice, payee, 40_000, key).headers["idempotent-replayed"] == "true"
    expire_keys(migrated_engine, key)
    again = send(client, alice, payee, 40_000, key)
    assert again.status_code == 201 and "idempotent-replayed" not in again.headers
    assert again.json()["id"] != first.json()["id"]
    # After expiry a different body is a new request, not a reuse error.
    expire_keys(migrated_engine, key)
    other = send(client, alice, payee, 7_000, key)
    assert other.status_code == 201
    assert balance(client, bob)["ledger"] == 87_000
    with Session(migrated_engine) as session:
        row = session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key["Idempotency-Key"])
        ).scalar_one()
        assert row.transaction_id == uuid.UUID(other.json()["id"])
        assert row.expires_at > utcnow() + timedelta(hours=23)


def test_an_expired_step_up_key_is_not_replayed(
    client: TestClient,
    waiting: tuple[Actor, Actor, dict[str, Any], str, str],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, bob, _payee, tid, secret = waiting
    key = new_key()
    url = f"/api/v1/transfers/{tid}/step-up"
    code = {"code": totp_code(secret, clock)}
    assert client.post(url, headers={**alice.headers, **key}, json=code).status_code == 200
    replay = client.post(url, headers={**alice.headers, **key}, json=code)
    assert replay.headers["idempotent-replayed"] == "true"
    expire_keys(migrated_engine, key)
    assert client.post(url, headers={**alice.headers, **key}, json=code).status_code == 409
    assert balance(client, bob)["ledger"] == 50_000


def test_an_expired_bill_payment_key_counts_as_unused(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(funds=500_000)
    key = new_key()
    body = {
        "from_account_id": str(alice.account_id),
        "biller_id": MSEB_BILLER_ID,
        "consumer_reference": "CA-987654321",
        "amount_minor": 1_000,
        "currency": "INR",
    }
    first = client.post("/api/v1/bills/pay", headers={**alice.headers, **key}, json=body)
    assert first.status_code == 201
    expire_keys(migrated_engine, key)
    second = client.post("/api/v1/bills/pay", headers={**alice.headers, **key}, json=body)
    assert second.status_code == 201 and "idempotent-replayed" not in second.headers
    assert second.json()["id"] != first.json()["id"]


def test_the_purge_deletes_only_expired_keys_and_never_their_transactions(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    alice, _bob, payee = pair
    force(app, FraudDecision.ALLOW)  # seven quick transfers would trip the velocity rule
    keys = [new_key() for _ in range(7)]
    for key in keys:
        assert send(client, alice, payee, 1_000, key).status_code == 201
    expire_keys(migrated_engine, *keys[:5])
    small_batches = settings.model_copy(update={"idempotency_purge_batch_size": 2})
    assert purge_expired_keys(migrated_engine, now=utcnow(), settings=small_batches) == 5
    with migrated_engine.connect() as connection:
        left = connection.execute(text("SELECT key FROM idempotency_keys")).scalars().all()
        assert sorted(left) == sorted(k["Idempotency-Key"] for k in keys[5:])
        transfers = connection.execute(
            text("SELECT count(*) FROM transactions WHERE type = 'TRANSFER'")
        ).scalar_one()
        assert transfers == 7
    assert purge_expired_keys(migrated_engine, now=utcnow(), settings=small_batches) == 0


def test_a_purged_scheduled_occurrence_key_never_pays_the_occurrence_twice(
    monkeypatch: pytest.MonkeyPatch,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
) -> None:
    """Background jobs rely on their own row state, not the key, once it has expired: the
    instruction has moved on to its next date, so a re-run the same day posts nothing."""
    monkeypatch.setattr(scheduled_service, "get_settings", lambda: settings)
    alice, _bob, payee = pair
    today = date(2026, 11, 1)
    with Session(migrated_engine) as session:
        instruction = ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=uuid.UUID(str(payee["id"])),
            amount=10_000,
            currency="INR",
            reference="rent",
            frequency=ScheduleFrequency.DAILY,
            status=ScheduledTransferStatus.ACTIVE,
            start_date=today,
            next_run_date=today,
        )
        session.add(instruction)
        session.commit()
        instruction_id = instruction.id
    with Session(migrated_engine) as session:
        outcome = execute_scheduled_instruction(session, instruction_id, today_utc=today)
        assert outcome == ScheduledExecutionOutcome.POSTED
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE idempotency_keys SET expires_at = now() - interval '1 s'"))
    assert purge_expired_keys(migrated_engine, now=utcnow(), settings=settings) == 1
    with Session(migrated_engine) as session:
        again = execute_scheduled_instruction(session, instruction_id, today_utc=today)
        assert again == ScheduledExecutionOutcome.SKIPPED_INACTIVE
        posted = session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.status == TransactionStatus.POSTED, Transaction.amount == 10_000)
        )
        assert posted == 1
