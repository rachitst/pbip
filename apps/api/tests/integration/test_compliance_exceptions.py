"""TASK-100 (UC-12 Compliance & Risk): exceptions, acknowledge/resolve, regulatory indicators.

The exceptions come from what the platform really records: transfers the fraud engine blocks,
lockouts after failed sign-ins, and a tampered audit chain. Times are moved back with SQL on the
mutable source rows (never on the append-only audit log) so the window and SLA counts are exact.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.db import utcnow
from pbip_api.core.errors import ConflictError
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.exceptions import (
    ACKNOWLEDGED_ACTION,
    CHAIN_CACHE_KEY,
    CHAIN_CACHE_TTL_SECONDS,
    RESOLVED_ACTION,
    RESOURCE_TYPE,
    ExceptionAction,
    ExceptionKind,
    apply_action,
)
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.compliance.service import AuditService
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.notifications.service import RetryPolicy, relay_outbox
from pbip_api.modules.payments import scheduled_service
from pbip_api.modules.payments.models import ScheduledTransfer, ScheduleFrequency
from pbip_api.modules.payments.scheduled_service import (
    ScheduledExecutionOutcome,
    execute_scheduled_instruction,
)
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import PASSWORD, Actor, FakeClock, age_payee
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

URL = "/api/v1/compliance/exceptions"
NO_EXCEPTIONS = {"OPEN": 0, "ACKNOWLEDGED": 0, "RESOLVED": 0}
ALL_KINDS = (
    "BLOCKED_TRANSFER",
    "ACCOUNT_LOCKOUT",
    "AUDIT_CHAIN_FAILURE",
    "NOTIFICATION_DEAD_LETTER",
    "SCHEDULED_TRANSFER_FAILURE",
)


@pytest.fixture(autouse=True)
def roomy_limits(settings: Settings) -> None:
    """Lockouts need more failed sign-ins per minute than the per-IP limit allows."""
    settings.rate_limit_login_per_minute = 1_000
    settings.rate_limit_register_per_hour = 1_000


def send(app: FastAPI, client: TestClient, sender: Actor, payee: dict[str, Any], decision: FraudDecision) -> str:  # fmt: skip
    app.dependency_overrides[get_fraud_scorer] = lambda: FixedDecisionScorer(decision)
    try:
        r = client.post(
            "/api/v1/transfers",
            headers={**sender.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
            json={"from_account_id": str(sender.account_id), "amount_minor": 12_345,
                  "payee_id": payee["id"], "reference": "rentzqx"},
        )  # fmt: skip
    finally:
        app.dependency_overrides.pop(get_fraud_scorer)
    assert r.status_code in (201, 202, 403), r.text
    return str(r.json().get("id", ""))


def lock_out(client: TestClient, email: str) -> None:
    for _ in range(5):  # login_max_failures: the fifth failure locks the account
        r = client.post("/api/v1/auth/login", json={"email": email, "password": "x" + PASSWORD})
        assert r.status_code == 401, r.text


def move_back(engine: Engine, table: str, row_id: str, hours: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET created_at = now() - make_interval(hours => :h) WHERE id = :id"),  # noqa: S608
            {"h": hours, "id": row_id},
        )  # fmt: skip


def event_of(engine: Engine, transaction_id: str) -> tuple[str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT e.id, a.id FROM fraud_events e JOIN fraud_alerts a "
                "ON a.fraud_event_id = e.id WHERE e.transaction_id = :t"
            ),
            {"t": transaction_id},
        ).one()
    return str(row[0]), str(row[1])


def lockout_ids(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id FROM security_events WHERE event_type = 'ACCOUNT_LOCKED'")
        ).all()
    return [str(r[0]) for r in rows]


def tamper_first_audit_row(engine: Engine) -> int:
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_immutable"))
        seq = int(connection.execute(text("SELECT min(seq) FROM audit_logs")).scalar_one())
        connection.execute(
            text("UPDATE audit_logs SET action = 'HACKED' WHERE seq = :s"), {"s": seq}
        )
        connection.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_immutable"))
    return seq  # fmt: skip


@pytest.fixture
def payer(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, dict[str, Any]]:
    sender = make_customer(funds=10_000_000)
    payee = make_verified_payee(sender, make_customer())
    age_payee(migrated_engine, payee["id"], 48)
    return sender, payee


def test_empty_dashboard_is_all_zeros_with_a_valid_chain(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    body = client.get(URL, headers=make_staff(RoleName.COMPLIANCE_OFFICER).headers).json()
    ind = body["indicators"]
    assert ind["window_days"] == 30 and ind["review_sla_hours"] == 24
    assert ind["acknowledge_sla_hours"] == 24
    assert (ind["unresolved_exceptions"], ind["unacknowledged_past_sla"]) == (0, 0)
    assert (ind["reviews_pending"], ind["reviews_past_sla"], ind["oldest_review_at"]) == (
        0,
        0,
        None,
    )
    assert ind["by_kind"] == dict.fromkeys(ALL_KINDS, NO_EXCEPTIONS)
    assert body["audit_chain"]["valid"] is True and body["audit_chain"]["first_invalid_seq"] is None
    assert body["items"] == []


def test_exceptions_and_indicators_are_exact_on_known_events(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    payer: tuple[Actor, dict[str, Any]],
) -> None:
    sender, payee = payer
    recent = send(app, client, sender, payee, FraudDecision.BLOCK)
    late = send(app, client, sender, payee, FraudDecision.BLOCK)
    old = send(app, client, sender, payee, FraudDecision.BLOCK)
    review = send(app, client, sender, payee, FraudDecision.REVIEW)
    send(app, client, sender, payee, FraudDecision.ALLOW)  # never an exception
    recent_event, _ = event_of(migrated_engine, recent)
    late_event, _ = event_of(migrated_engine, late)
    old_event, old_alert = event_of(migrated_engine, old)
    _, review_alert = event_of(migrated_engine, review)
    move_back(migrated_engine, "fraud_events", late_event, 30)  # in window, past acknowledge SLA
    move_back(migrated_engine, "fraud_events", old_event, 31 * 24)  # outside the 30-day window
    move_back(migrated_engine, "fraud_alerts", review_alert, 25)  # review past its 24 h SLA
    move_back(migrated_engine, "fraud_alerts", old_alert, 31 * 24)  # and so is this one
    locked = make_customer()
    lock_out(client, locked.email)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)

    body = client.get(URL, headers=officer.headers).json()

    ind = body["indicators"]
    assert ind["by_kind"]["BLOCKED_TRANSFER"] == {"OPEN": 2, "ACKNOWLEDGED": 0, "RESOLVED": 0}
    assert ind["by_kind"]["ACCOUNT_LOCKOUT"] == {"OPEN": 1, "ACKNOWLEDGED": 0, "RESOLVED": 0}
    assert ind["by_kind"]["AUDIT_CHAIN_FAILURE"] == NO_EXCEPTIONS
    assert ind["unresolved_exceptions"] == 3 and ind["unacknowledged_past_sla"] == 1
    # every BLOCK and REVIEW opened an alert: 4 open, 2 of them older than 24 h
    assert (ind["reviews_pending"], ind["reviews_past_sla"]) == (4, 2)
    lockout = lockout_ids(migrated_engine)
    assert [(i["kind"], i["ref"]) for i in body["items"]] == [
        ("ACCOUNT_LOCKOUT", lockout[0]),
        ("BLOCKED_TRANSFER", recent_event),
        ("BLOCKED_TRANSFER", late_event),
    ]
    blocked = body["items"][1]
    assert blocked["id"] == f"BLOCKED_TRANSFER:{recent_event}" and blocked["status"] == "OPEN"
    assert (blocked["risk_score"], blocked["amount_minor"], blocked["currency"]) == (
        95,
        12_345,
        "INR",
    )
    assert blocked["transaction_type"] == "TRANSFER"
    assert blocked["reason_codes"] == ["TEST_FIXED_DECISION"]
    assert blocked["acknowledged"] is None and blocked["resolved"] is None

    wide = client.get(f"{URL}?days=90&kind=BLOCKED_TRANSFER", headers=officer.headers).json()
    assert [i["ref"] for i in wide["items"]] == [recent_event, late_event, old_event]
    assert wide["indicators"]["by_kind"]["BLOCKED_TRANSFER"]["OPEN"] == 3


def test_acknowledge_then_resolve_is_audited_with_actor_and_reason(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    db: Session,
    payer: tuple[Actor, dict[str, Any]],
) -> None:
    sender, payee = payer
    event, _ = event_of(migrated_engine, send(app, client, sender, payee, FraudDecision.BLOCK))
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    base = f"{URL}/BLOCKED_TRANSFER/{event}"

    acked = client.post(
        f"{base}/acknowledge", headers=officer.headers, json={"reason": "Looking into it"}
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["status"] == "ACKNOWLEDGED"
    assert acked.json()["acknowledged"]["by_user_id"] == str(officer.user_id)
    again = client.post(f"{base}/acknowledge", headers=officer.headers, json={"reason": "twice"})
    assert again.status_code == 409

    second = make_staff(RoleName.ADMIN)
    done = client.post(
        f"{base}/resolve",
        headers=second.headers,
        json={"reason": "  Customer confirmed the block  "},
    )
    assert done.status_code == 200, done.text
    out = done.json()
    assert (
        out["status"] == "RESOLVED" and out["resolved"]["reason"] == "Customer confirmed the block"
    )
    assert out["resolved"]["by_user_id"] == str(second.user_id)
    assert out["acknowledged"]["reason"] == "Looking into it"
    assert client.post(f"{base}/resolve", headers=officer.headers, json={"reason": "again"}).status_code == 409  # fmt: skip

    rows = db.execute(
        select(AuditLog).where(AuditLog.resource_type == RESOURCE_TYPE).order_by(AuditLog.seq)
    ).scalars().all()  # fmt: skip
    assert [(r.action, r.actor_user_id, r.resource_id) for r in rows] == [
        (ACKNOWLEDGED_ACTION, officer.user_id, f"BLOCKED_TRANSFER:{event}"),
        (RESOLVED_ACTION, second.user_id, f"BLOCKED_TRANSFER:{event}"),
    ]
    assert rows[0].details == {"kind": "BLOCKED_TRANSFER", "ref": event, "previous_status": "OPEN", "reason": "Looking into it"}  # fmt: skip
    assert rows[1].details["previous_status"] == "ACKNOWLEDGED"
    assert all(r.request_id for r in rows)

    listed = client.get(f"{URL}?status=RESOLVED", headers=officer.headers).json()
    assert [i["ref"] for i in listed["items"]] == [event]
    assert listed["indicators"]["unresolved_exceptions"] == 0
    assert client.get(f"{URL}?status=OPEN", headers=officer.headers).json()["items"] == []


def test_an_open_lockout_can_be_resolved_directly(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:  # fmt: skip
    lock_out(client, make_customer().email)
    (ref,) = lockout_ids(migrated_engine)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    r = client.post(f"{URL}/ACCOUNT_LOCKOUT/{ref}/resolve", headers=officer.headers, json={"reason": "Customer called"})  # fmt: skip
    assert r.status_code == 200 and r.json()["acknowledged"] is None
    counts = client.get(URL, headers=officer.headers).json()["indicators"]["by_kind"]
    assert counts["ACCOUNT_LOCKOUT"] == {"OPEN": 0, "ACKNOWLEDGED": 0, "RESOLVED": 1}


def test_a_tampered_audit_chain_is_an_exception_that_can_be_worked(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:  # fmt: skip
    make_customer()
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    seq = tamper_first_audit_row(migrated_engine)

    body = client.get(URL, headers=officer.headers).json()
    assert body["audit_chain"]["valid"] is False and body["audit_chain"]["first_invalid_seq"] == seq
    (item,) = body["items"]
    assert (item["kind"], item["ref"], item["status"], item["first_invalid_seq"]) == ("AUDIT_CHAIN_FAILURE", str(seq), "OPEN", seq)  # fmt: skip
    assert body["indicators"]["by_kind"]["AUDIT_CHAIN_FAILURE"]["OPEN"] == 1
    # a standing condition: listed whatever the window
    assert len(client.get(f"{URL}?days=1", headers=officer.headers).json()["items"]) == 1

    ack = client.post(f"{URL}/AUDIT_CHAIN_FAILURE/{seq}/acknowledge", headers=officer.headers, json={"reason": "Escalated to security"})  # fmt: skip
    assert ack.status_code == 200 and ack.json()["status"] == "ACKNOWLEDGED"
    again = client.get(URL, headers=officer.headers).json()
    assert again["indicators"]["by_kind"]["AUDIT_CHAIN_FAILURE"] == {"OPEN": 0, "ACKNOWLEDGED": 1, "RESOLVED": 0}  # fmt: skip
    # another sequence number is not an exception that exists
    other = client.post(f"{URL}/AUDIT_CHAIN_FAILURE/{seq + 1000}/acknowledge", headers=officer.headers, json={"reason": "nope"})  # fmt: skip
    assert other.status_code == 404


def test_page_loads_reuse_the_cached_chain_verification(
    client: TestClient,
    make_staff: Callable[..., Actor],
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0027 condition: no full audit-log scan per page load. The chain is verified once and
    reused for CHAIN_CACHE_TTL_SECONDS; when the entry expires the next load verifies again."""
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    calls: list[int] = []
    real = AuditService.verify_chain

    def counting(self: AuditService) -> Any:
        calls.append(1)
        return real(self)

    monkeypatch.setattr(AuditService, "verify_chain", counting)
    for _ in range(3):
        assert client.get(URL, headers=officer.headers).status_code == 200
    assert len(calls) == 1
    assert fake_redis.ttl(CHAIN_CACHE_KEY) <= CHAIN_CACHE_TTL_SECONDS
    fake_redis.delete(CHAIN_CACHE_KEY)  # what expiry does
    assert client.get(URL, headers=officer.headers).status_code == 200
    assert len(calls) == 2


def test_unknown_or_malformed_exceptions_are_404_and_bad_input_is_422(
    app: FastAPI,
    client: TestClient,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    payer: tuple[Actor, dict[str, Any]],
) -> None:
    sender, payee = payer
    allowed = send(app, client, sender, payee, FraudDecision.REVIEW)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    review_event, _ = event_of(migrated_engine, allowed)
    reason = {"reason": "checked"}
    for path in (
        f"BLOCKED_TRANSFER/{uuid.uuid4()}",
        f"BLOCKED_TRANSFER/{review_event}",  # a REVIEW decision is not a blocked transfer
        f"ACCOUNT_LOCKOUT/{review_event}",  # a fraud event is not a lockout
        "ACCOUNT_LOCKOUT/not-a-uuid",
        "AUDIT_CHAIN_FAILURE/1",  # the chain is valid
    ):
        r = client.post(f"{URL}/{path}/acknowledge", headers=officer.headers, json=reason)
        assert r.status_code == 404, path
        assert "not-a-uuid" not in r.text  # no echo of user input
    for body in ({}, {"reason": "ab"}, {"reason": "x" * 501}, {"reason": "ok\x00no"}, {"reason": "fine", "extra": 1}):  # fmt: skip
        r = client.post(f"{URL}/BLOCKED_TRANSFER/{uuid.uuid4()}/acknowledge", headers=officer.headers, json=body)  # fmt: skip
        assert r.status_code == 422, body
    assert client.post(f"{URL}/NOT_A_KIND/1/acknowledge", headers=officer.headers, json=reason).status_code == 422  # fmt: skip
    assert client.get(f"{URL}?days=91", headers=officer.headers).status_code == 422
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM audit_logs WHERE resource_type = :t"), {"t": RESOURCE_TYPE}).scalar_one() == 0  # fmt: skip


def test_compliance_exceptions_need_compliance_or_admin_with_mfa(
    client: TestClient, make_customer: Callable[..., Actor], make_staff: Callable[..., Actor]
) -> None:
    ack = f"{URL}/ACCOUNT_LOCKOUT/{uuid.uuid4()}/acknowledge"
    reason = {"reason": "checked"}
    assert client.get(URL).status_code == 401
    assert client.post(ack, json=reason).status_code == 401
    for actor in (
        make_customer(),
        make_staff(RoleName.FRAUD_ANALYST),
        make_staff(RoleName.RELATIONSHIP_MANAGER),
    ):
        assert client.get(URL, headers=actor.headers).status_code == 403
        assert client.post(ack, headers=actor.headers, json=reason).status_code == 403
    no_mfa = make_staff(RoleName.COMPLIANCE_OFFICER, mfa=False)
    denied = client.get(URL, headers=no_mfa.headers)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    assert client.post(ack, headers=no_mfa.headers, json=reason).json()["code"] == "mfa_required"
    for role in (RoleName.COMPLIANCE_OFFICER, RoleName.ADMIN):
        assert client.get(URL, headers=make_staff(role).headers).status_code == 200


def test_exceptions_response_carries_no_customer_pii(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    payer: tuple[Actor, dict[str, Any]],
) -> None:
    sender, payee = payer
    send(app, client, sender, payee, FraudDecision.BLOCK)
    locked = make_customer()
    lock_out(client, locked.email)
    body = client.get(URL, headers=make_staff(RoleName.COMPLIANCE_OFFICER).headers).text
    assert len(client.get(URL, headers=make_staff(RoleName.ADMIN).headers).json()["items"]) == 2
    for secret in (sender.email, locked.email, "Test Customer", sender.account_number,
                   str(sender.customer_id), str(locked.user_id),
                   str(sender.user_id), "rentzqx"):  # fmt: skip
        assert secret and secret not in body, secret


def test_concurrent_acknowledgements_record_exactly_one(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    """Two officers acting on the same open exception at once: one wins, the other gets 409."""
    lock_out(client, make_customer().email)
    (ref,) = lockout_ids(migrated_engine)
    officers = [make_staff(RoleName.COMPLIANCE_OFFICER) for _ in range(4)]
    start = threading.Barrier(len(officers))

    def acknowledge(officer: Actor) -> str:
        with sessionmaker(bind=migrated_engine)() as session:
            start.wait()
            try:
                apply_action(session, ExceptionKind.ACCOUNT_LOCKOUT, ref, ExceptionAction.ACKNOWLEDGE, "mine", officer.user_id)  # fmt: skip
            except ConflictError:
                return "conflict"
            return "ok"

    with ThreadPoolExecutor(max_workers=len(officers)) as pool:
        outcomes = sorted(pool.map(acknowledge, officers))
    assert outcomes == ["conflict", "conflict", "conflict", "ok"]
    with migrated_engine.connect() as connection:
        n = connection.execute(text("SELECT count(*) FROM audit_logs WHERE action = :a"), {"a": ACKNOWLEDGED_ACTION}).scalar_one()  # fmt: skip
    assert n == 1


# ---------------------------------------------------------------- system exceptions (section 5)


def dead_letter(db: Session, user_id: uuid.UUID) -> tuple[str, str]:
    """A real dead letter: the relay fails an unhandled event on its only attempt (ADR-0040).
    Returns (audit seq of the dead-letter row, outbox event id)."""
    event = OutboxEvent(event_type="NOT_A_HANDLED_TYPE", payload={"user_id": str(user_id)})
    db.add(event)
    db.commit()
    assert relay_outbox(db, now=utcnow(), policy=RetryPolicy(max_attempts=1)) == 1
    db.commit()
    seq = db.scalars(
        select(AuditLog.seq).where(AuditLog.action == "OUTBOX_EVENT_DEAD_LETTERED")
    ).one()
    return str(seq), str(event.id)


def missed_occurrence(
    engine: Engine, sender: Actor, payee: dict[str, Any], settings: Settings
) -> tuple[str, str]:
    """A real missed occurrence: unfunded yesterday, recovered as MISSED today (ADR-0023). The
    amount is above the sender's balance but inside the per-transfer and daily limits.
    Returns (audit seq of the MISSED row, scheduled transfer id)."""
    today = utcnow().date()
    yesterday = today - timedelta(days=1)
    with Session(engine) as session:
        order = ScheduledTransfer(
            customer_id=sender.customer_id, user_id=sender.user_id,
            from_account_id=sender.account_id, payee_id=uuid.UUID(str(payee["id"])),
            amount=15_000_000, currency="INR", reference="rent", frequency=ScheduleFrequency.DAILY,
            start_date=yesterday, next_run_date=yesterday,
        )  # fmt: skip
        session.add(order)
        session.commit()
        order_id = order.id
    with patch.object(scheduled_service, "get_settings", lambda: settings):
        for day, expected in (
            (yesterday, ScheduledExecutionOutcome.RETRYABLE_FAILURE),
            (today, ScheduledExecutionOutcome.RECOVERED),
        ):
            with Session(engine) as session:
                assert execute_scheduled_instruction(session, order_id, today_utc=day) == expected
    with Session(engine) as session:
        seq = session.scalars(
            select(AuditLog.seq).where(AuditLog.action == "SCHEDULED_TRANSFER_MISSED")
        ).one()
    return str(seq), str(order_id)


def test_dead_lettered_notifications_are_exceptions_without_their_payload(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    alice = make_customer()
    seq, event_id = dead_letter(db, alice.user_id)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)

    body = client.get(URL, headers=officer.headers).json()

    assert body["indicators"]["by_kind"]["NOTIFICATION_DEAD_LETTER"] == {**NO_EXCEPTIONS, "OPEN": 1}
    assert body["indicators"]["unresolved_exceptions"] == 1
    (item,) = body["items"]
    assert (item["id"], item["kind"], item["ref"]) == (
        f"NOTIFICATION_DEAD_LETTER:{seq}", "NOTIFICATION_DEAD_LETTER", seq,
    )  # fmt: skip
    assert (item["subject_id"], item["event_type"], item["attempts"], item["error_type"]) == (
        event_id, "NOT_A_HANDLED_TYPE", 1, "NoOutboxHandlerError",
    )  # fmt: skip
    assert str(alice.user_id) not in str(item)  # the payload is never shown
    assert item["failure"] is None and item["amount_minor"] is None

    acked = client.post(
        f"{URL}/NOTIFICATION_DEAD_LETTER/{seq}/acknowledge",
        headers=officer.headers,
        json={"reason": "Handler missing; raised with the platform team"},
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["status"] == "ACKNOWLEDGED" and acked.json()["subject_id"] == event_id
    rows = db.scalars(
        select(AuditLog).where(AuditLog.resource_type == RESOURCE_TYPE)
    ).all()  # fmt: skip
    assert [(r.action, r.actor_user_id, r.resource_id) for r in rows] == [
        (ACKNOWLEDGED_ACTION, officer.user_id, f"NOTIFICATION_DEAD_LETTER:{seq}")
    ]
    listed = client.get(f"{URL}?kind=NOTIFICATION_DEAD_LETTER&status=ACKNOWLEDGED", headers=officer.headers)  # fmt: skip
    assert [i["ref"] for i in listed.json()["items"]] == [seq]


def test_missed_and_auto_paused_scheduled_transfers_are_exceptions(
    client: TestClient,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    db: Session,
    settings: Settings,
    payer: tuple[Actor, dict[str, Any]],
) -> None:
    sender, payee = payer
    missed_seq, order_id = missed_occurrence(migrated_engine, sender, payee, settings)
    # The permanent-failure path (payments.scheduled_service) writes this row; recorded here the
    # same way, because forcing a permanent transfer failure needs a broken account.
    paused = AuditService(db).record(
        None, "SCHEDULED_TRANSFER_AUTO_PAUSED", "scheduled_transfer", order_id,
        details={"actor": "scheduler", "channel": "SCHEDULED", "reason": "account_not_active"},
    )  # fmt: skip
    db.commit()
    officer = make_staff(RoleName.ADMIN)

    body = client.get(f"{URL}?kind=SCHEDULED_TRANSFER_FAILURE", headers=officer.headers).json()

    assert body["indicators"]["by_kind"]["SCHEDULED_TRANSFER_FAILURE"]["OPEN"] == 2
    paused_item, missed_item = body["items"]
    assert (paused_item["ref"], paused_item["failure"], paused_item["failure_reason"]) == (
        str(paused.seq), "AUTO_PAUSED", "account_not_active",
    )  # fmt: skip
    assert paused_item["occurrence_date"] is None
    assert missed_item["ref"] == missed_seq and missed_item["subject_id"] == order_id
    assert (missed_item["failure"], missed_item["failure_reason"]) == (
        "MISSED",
        "insufficient_funds",
    )
    assert missed_item["occurrence_date"] == (utcnow().date() - timedelta(days=1)).isoformat()
    assert missed_item["failed_attempts"] == 1

    done = client.post(
        f"{URL}/SCHEDULED_TRANSFER_FAILURE/{missed_seq}/resolve",
        headers=officer.headers,
        json={"reason": "Customer told; order continues"},
    )
    assert done.status_code == 200 and done.json()["status"] == "RESOLVED"
    again = client.post(f"{URL}/SCHEDULED_TRANSFER_FAILURE/{missed_seq}/resolve", headers=officer.headers, json={"reason": "again"})  # fmt: skip
    assert again.status_code == 409


def test_a_system_exception_reference_must_be_a_matching_audit_row(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    """Wrong kind, a sequence of an unrelated audit row, or a malformed or huge number: 404."""
    seq, _ = dead_letter(db, make_customer().user_id)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    unrelated = db.scalars(
        select(AuditLog.seq).where(AuditLog.action != "OUTBOX_EVENT_DEAD_LETTERED")
    ).first()
    assert unrelated is not None
    for kind, ref in (
        ("SCHEDULED_TRANSFER_FAILURE", seq),  # it is a dead letter, not a scheduled failure
        ("NOTIFICATION_DEAD_LETTER", str(unrelated)),
        ("NOTIFICATION_DEAD_LETTER", "0"),
        ("NOTIFICATION_DEAD_LETTER", "9" * 30),  # beyond bigint: no row, and no SQL error
        ("NOTIFICATION_DEAD_LETTER", "abc"),
        ("BLOCKED_TRANSFER", seq),  # a sequence is not a fraud event id
    ):
        r = client.post(f"{URL}/{kind}/{ref}/acknowledge", headers=officer.headers, json={"reason": "check"})  # fmt: skip
        assert r.status_code == 404, (kind, ref, r.text)


def test_system_exceptions_follow_the_window(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
    clock: FakeClock,
) -> None:
    """The audit row is append-only, so the clock moves instead: 31 days on, it is outside 30."""
    dead_letter(db, make_customer().user_id)
    officer = make_staff(RoleName.COMPLIANCE_OFFICER)
    clock.advance(31 * 24 * 3600)
    month = client.get(URL, headers=officer.headers).json()
    quarter = client.get(f"{URL}?days=90", headers=officer.headers).json()
    assert month["indicators"]["by_kind"]["NOTIFICATION_DEAD_LETTER"] == NO_EXCEPTIONS
    assert month["items"] == []
    assert quarter["indicators"]["by_kind"]["NOTIFICATION_DEAD_LETTER"]["OPEN"] == 1
    assert quarter["indicators"]["unacknowledged_past_sla"] == 1
