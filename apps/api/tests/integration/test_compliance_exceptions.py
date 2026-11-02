"""TASK-100 (UC-12 Compliance & Risk): exceptions, acknowledge/resolve, regulatory indicators.

The exceptions come from what the platform really records: transfers the fraud engine blocks,
lockouts after failed sign-ins, and a tampered audit chain. Times are moved back with SQL on the
mutable source rows (never on the append-only audit log) so the window and SLA counts are exact.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
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
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import PASSWORD, Actor, age_payee
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

URL = "/api/v1/compliance/exceptions"
NO_EXCEPTIONS = {"OPEN": 0, "ACKNOWLEDGED": 0, "RESOLVED": 0}


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
    assert ind["by_kind"] == dict.fromkeys(("BLOCKED_TRANSFER", "ACCOUNT_LOCKOUT", "AUDIT_CHAIN_FAILURE"), NO_EXCEPTIONS)  # fmt: skip
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
