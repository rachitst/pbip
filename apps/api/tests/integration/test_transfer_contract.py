"""The exact contract of a transfer: the response body, the audit row, what the fraud engine is
given, the metrics, the notifications, the idempotency row and the wording of every error.

TASK-086 (mutation testing) showed these were only loosely asserted: a renamed response field, a
dropped audit detail or a fraud context missing the device or network all went unnoticed.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError, UnprocessableError
from pbip_api.modules.accounts.models import Account, AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.customers.models import Customer
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import IdempotencyKey, Transaction
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.payees.models import Payee, PayeeStatus
from pbip_api.modules.payments.service import (
    IdempotencyKeyReuseError,
    LimitExceededError,
    PaymentsService,
    TransferCommand,
)
from pbip_ml.fraud.contract import FraudDecision, FraudScoreResult, TransactionContext
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, age_payee
from prometheus_client import REGISTRY
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

_IP = "203.0.113.7"
_META = RequestMeta(ip=_IP, user_agent="contract-test", request_id="req-contract-1")


class RecordingScorer:
    """Returns a fixed decision with a known latency and keeps every context it was given."""

    model_version = "test-recording"

    def __init__(self, decision: FraudDecision = FraudDecision.ALLOW) -> None:
        self.decision = decision
        self.contexts: list[TransactionContext] = []

    def score(self, context: TransactionContext) -> FraudScoreResult:
        self.contexts.append(context)
        inner = FixedDecisionScorer(self.decision).score(context)
        return FraudScoreResult(
            risk_score=inner.risk_score,
            decision=inner.decision,
            reasons=inner.reasons,
            model_version=self.model_version,
            latency_ms=250.0,
        )


class OneCountryResolver:
    def country(self, ip: str | None) -> str | None:
        return "GB" if ip == _IP else None


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, bob, payee


def _command(
    alice: Actor,
    amount: int = 10_000,
    *,
    payee_id: uuid.UUID | None = None,
    to_account_id: uuid.UUID | None = None,
    currency: str = "INR",
    reference: str | None = "rent",
) -> TransferCommand:
    assert alice.account_id is not None
    return TransferCommand(
        from_account_id=alice.account_id,
        amount_minor=amount,
        currency=currency,
        payee_id=payee_id,
        to_account_id=to_account_id,
        reference=reference,
    )


def _create(
    engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    alice: Actor,
    command: TransferCommand,
    *,
    scorer: RecordingScorer | None = None,
    key: str | None = None,
    device_id: uuid.UUID | None = None,
) -> Any:
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        customer = session.get(Customer, alice.customer_id)
        assert customer is not None
        service = PaymentsService(
            session,
            settings,
            scorer or RecordingScorer(),
            auth_service_factory(session),
            countries=OneCountryResolver(),
        )
        return service.create_transfer(
            user_id=alice.user_id,
            customer=customer,
            command=command,
            idempotency_key=key or f"key-{uuid.uuid4().hex}",
            meta=_META,
            device_id=device_id,
        )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# ------------------------------------------------------------------------- response body
def test_a_created_transfer_returns_the_exact_contract(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, _bob, payee = pair
    response = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": f"key-{uuid.uuid4().hex}"},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 12_345,
            "payee_id": payee["id"],
            "reference": "rent",
        },
    )
    assert response.status_code == 201
    with sessionmaker(bind=migrated_engine)() as session:
        row = session.get(Transaction, uuid.UUID(response.json()["id"]))
        assert row is not None and row.posted_at is not None
        assert str(row.payee_id) == payee["id"]
        expected = {
            "id": str(row.id),
            "status": "POSTED",
            "amount_minor": 12_345,
            "currency": "INR",
            "from_account_id": str(alice.account_id),
            "to_account_id": None,
            "payee_id": payee["id"],
            "reference": "rent",
            "created_at": row.created_at.isoformat(),
            "posted_at": row.posted_at.isoformat(),
            "step_up_required": False,
            "message": "Transfer completed",
        }
    assert response.json() == expected


def test_a_transfer_between_own_accounts_reports_no_payee(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(funds=50_000)
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
        second = open_account(session, alice.customer_id, AccountType.CURRENT)  # type: ignore[arg-type]
        session.commit()
    body = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": f"key-{uuid.uuid4().hex}"},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 1_000,
            "to_account_id": str(second.id),
        },
    ).json()
    assert body["payee_id"] is None and body["reference"] is None
    with sessionmaker(bind=migrated_engine)() as session:
        row = session.get(Transaction, uuid.UUID(body["id"]))
        assert row is not None and row.payee_id is None and row.credit_account_id == second.id


# --------------------------------------------------- audit, fraud context, metrics, outbox
def test_creating_a_transfer_audits_scores_counts_and_notifies_exactly(
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    alice, _bob, payee = pair
    scorer = RecordingScorer(FraudDecision.ALLOW)
    device = uuid.uuid4()
    posted = _sample("transfers_total", {"status": "POSTED"})
    allowed = _sample("fraud_decisions_total", {"decision": "ALLOW"})
    latency = _sample("fraud_inference_seconds_sum")

    result = _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 7_000, payee_id=uuid.UUID(payee["id"])),
        scorer=scorer,
        device_id=device,
    )

    tid = result.body["id"]
    [context] = scorer.contexts
    assert str(context.transaction_id) == tid and str(context.payee_id) == payee["id"]
    assert context.device_id == device
    assert context.ip_country == "GB" and context.ip_network == "203.0.113.0/24"
    assert context.customer_id == alice.customer_id and context.account_id == alice.account_id
    assert _sample("transfers_total", {"status": "POSTED"}) == posted + 1
    assert _sample("fraud_decisions_total", {"decision": "ALLOW"}) == allowed + 1
    assert _sample("fraud_inference_seconds_sum") == pytest.approx(latency + 0.25, abs=1e-9)
    with sessionmaker(bind=migrated_engine)() as session:
        audit = session.execute(
            select(AuditLog).where(
                AuditLog.action == "TRANSFER_CREATED", AuditLog.resource_id == tid
            )
        ).scalar_one()
        assert audit.actor_user_id == alice.user_id and audit.resource_type == "transaction"
        assert audit.ip == _IP and audit.request_id == "req-contract-1"
        assert audit.details == {
            "status": "POSTED",
            "decision": "ALLOW",
            "risk_score": 5,
            "amount_minor": 7_000,
        }
        row = session.get(Transaction, uuid.UUID(tid))
        assert row is not None and str(row.payee_id) == payee["id"]


@pytest.mark.parametrize(
    ("decision", "event_type", "status"),
    [
        (FraudDecision.ALLOW, "TRANSFER_POSTED", "POSTED"),
        (FraudDecision.REVIEW, "TRANSFER_UNDER_REVIEW", "PENDING_REVIEW"),
        (FraudDecision.BLOCK, "TRANSFER_DECLINED", "DECLINED"),
    ],
)
def test_every_outcome_notifies_the_customer_who_sent_it(
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    decision: FraudDecision,
    event_type: str,
    status: str,
) -> None:
    alice, _bob, payee = pair
    before = _sample("transfers_total", {"status": status})
    result = _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 3_000, payee_id=uuid.UUID(payee["id"])),
        scorer=RecordingScorer(decision),
    )
    assert result.body["status"] == status
    assert _sample("transfers_total", {"status": status}) == before + 1
    with sessionmaker(bind=migrated_engine)() as session:
        event = session.execute(
            select(OutboxEvent).where(
                OutboxEvent.payload["transaction_id"].astext == result.body["id"]
            )
        ).scalar_one()
    assert event.event_type == event_type
    assert event.payload["user_id"] == str(alice.user_id)


# ------------------------------------------------------------------------- idempotency
def test_the_idempotency_row_stores_this_users_request_hash_and_expires_later(
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    alice, _bob, payee = pair
    command = _command(alice, 2_000, payee_id=uuid.UUID(payee["id"]))
    key = f"key-{uuid.uuid4().hex}"
    _create(migrated_engine, settings, auth_service_factory, alice, command, key=key)
    with sessionmaker(bind=migrated_engine)() as session:
        row = session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)).scalar_one()
    assert row.request_hash == PaymentsService._request_hash(alice.user_id, command)
    assert row.expires_at > utcnow() + timedelta(hours=settings.idempotency_ttl_hours - 1)


def test_a_replay_finds_its_own_row_among_other_keys_and_other_users(
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    """Keys are scoped per user: Carol reusing Alice's key, and Alice's other keys, must not
    confuse the lookup of the stored response."""
    alice, bob, payee = pair
    carol = make_customer(funds=100_000)
    carol_payee = make_verified_payee(carol, bob)
    key = f"key-{uuid.uuid4().hex}"
    alice_cmd = _command(alice, 1_500, payee_id=uuid.UUID(payee["id"]))
    first = _create(migrated_engine, settings, auth_service_factory, alice, alice_cmd, key=key)
    _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 1_600, payee_id=uuid.UUID(payee["id"])),
    )
    _create(
        migrated_engine,
        settings,
        auth_service_factory,
        carol,
        _command(carol, 1_700, payee_id=uuid.UUID(carol_payee["id"])),
        key=key,
    )
    replay = _create(migrated_engine, settings, auth_service_factory, alice, alice_cmd, key=key)
    assert replay.replayed and replay.body == first.body


def _seed_key(
    engine: Engine, alice: Actor, key: str, request_hash: str, status: int | None
) -> None:
    with sessionmaker(bind=engine)() as session:
        session.add(
            IdempotencyKey(
                id=uuid.uuid4(),
                user_id=alice.user_id,
                key=key,
                request_hash=request_hash,
                expires_at=utcnow() + timedelta(hours=1),
                response_status=status,
                response_body=None,
            )
        )
        session.commit()


@pytest.mark.parametrize("stored_status", [None, 201])
def test_a_key_without_a_complete_stored_response_is_still_in_progress(
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
    stored_status: int | None,
) -> None:
    alice, _bob, payee = pair
    command = _command(alice, 1_000, payee_id=uuid.UUID(payee["id"]))
    key = f"key-{uuid.uuid4().hex}"
    _seed_key(
        migrated_engine,
        alice,
        key,
        PaymentsService._request_hash(alice.user_id, command),
        stored_status,
    )
    with pytest.raises(
        ConflictError, match=r"^a request with this Idempotency-Key is still in progress$"
    ):
        _create(migrated_engine, settings, auth_service_factory, alice, command, key=key)


def test_reusing_a_key_for_a_different_request_names_the_problem(
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    alice, _bob, payee = pair
    key = f"key-{uuid.uuid4().hex}"
    pid = uuid.UUID(payee["id"])
    _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 100, payee_id=pid),
        key=key,
    )
    with pytest.raises(
        IdempotencyKeyReuseError,
        match=r"^Idempotency-Key was already used with a different request$",
    ):
        _create(
            migrated_engine,
            settings,
            auth_service_factory,
            alice,
            _command(alice, 101, payee_id=pid),
            key=key,
        )


# ------------------------------------------------------------------------- error wording
def _point_payee_at(engine: Engine, payee_id: str, account_number: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(Payee)
            .where(Payee.id == uuid.UUID(payee_id))
            .values(account_number=account_number)
        )


def test_destination_errors_carry_their_exact_messages(
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    alice, _bob, payee = pair
    pid = uuid.UUID(payee["id"])

    def attempt(command: TransferCommand) -> None:
        _create(migrated_engine, settings, auth_service_factory, alice, command)

    with pytest.raises(LimitExceededError, match=r"^amount exceeds the per-transfer limit$"):
        attempt(_command(alice, settings.max_transfer_minor + 1, payee_id=pid))
    with pytest.raises(UnprocessableError, match=r"^currency does not match the source account$"):
        attempt(_command(alice, 100, payee_id=pid, currency="USD"))
    with pytest.raises(UnprocessableError, match=r"^source and destination must differ$"):
        attempt(_command(alice, 100, to_account_id=alice.account_id))

    assert alice.account_number is not None
    _point_payee_at(migrated_engine, payee["id"], alice.account_number)
    with pytest.raises(UnprocessableError, match=r"^source and destination must differ$"):
        attempt(_command(alice, 100, payee_id=pid))

    with sessionmaker(bind=migrated_engine)() as session:
        clearing = session.get(Account, ledger.SYSTEM_CLEARING_ID)
        assert clearing is not None and clearing.account_type == AccountType.SYSTEM
        clearing_number = clearing.account_number
    _point_payee_at(migrated_engine, payee["id"], clearing_number)  # never a payee destination
    with pytest.raises(UnprocessableError, match=r"^payee account is unavailable$"):
        attempt(_command(alice, 100, payee_id=pid))

    with migrated_engine.begin() as conn:
        conn.execute(
            update(Payee).where(Payee.id == pid).values(status=PayeeStatus.PENDING_VERIFICATION)
        )
    with pytest.raises(UnprocessableError, match=r"^payee must be verified before sending money$"):
        attempt(_command(alice, 100, payee_id=pid))


# ------------------------------------------------------------------------- daily limit
def test_the_daily_limit_counts_only_this_users_live_transfers(
    app: FastAPI,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    settings: Settings,
    auth_service_factory: Callable[[Session], AuthService],
) -> None:
    """Another customer's spending and Alice's own declined transfer must not use up her limit."""
    alice, bob, payee = pair
    carol = make_customer(funds=1_000_000)
    carol_payee = make_verified_payee(carol, bob)
    settings.daily_transfer_limit_minor = 100_000
    pid = uuid.UUID(payee["id"])
    carol_spent = _create(
        migrated_engine,
        settings,
        auth_service_factory,
        carol,
        _command(carol, 90_000, payee_id=uuid.UUID(carol_payee["id"])),
    )
    assert carol_spent.body["status"] == "POSTED"
    blocked = _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 90_000, payee_id=pid),
        scorer=RecordingScorer(FraudDecision.BLOCK),
    )
    assert blocked.body["status"] == "DECLINED"
    allowed = _create(
        migrated_engine,
        settings,
        auth_service_factory,
        alice,
        _command(alice, 90_000, payee_id=pid),
    )
    assert allowed.body["status"] == "POSTED"
