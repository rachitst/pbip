"""A fraud analyst clears a FRAUD_BLOCK pause on a standing order (issue #304, ADR-0023 R1/R5).

Clearing leaves the order PAUSED (reason CUSTOMER): it never restarts unattended, the customer
resumes it. Each clear is audited with the analyst and the reason, and the customer is told in
the bell without any score or reason.
"""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.notifications.models import Notification, OutboxEvent
from pbip_api.modules.notifications.service import relay_outbox
from pbip_api.modules.payments.models import (
    ScheduledTransfer,
    ScheduledTransferStatus,
    ScheduleFrequency,
    SchedulePauseReason,
)
from pbip_test_support import Actor, age_payee
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]

AMOUNT = 15_000
ACTION = "SCHEDULED_TRANSFER_FRAUD_BLOCK_CLEARED"
REASON = {"reason": "Customer confirmed the payee by phone"}


def _url(scheduled_transfer_id: uuid.UUID) -> str:
    return f"/api/v1/fraud/scheduled-transfers/{scheduled_transfer_id}/clear"


@pytest.fixture
def alice(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> Actor:
    alice = make_customer(funds=1_000_000)
    payee = make_verified_payee(alice, make_customer(funds=0))
    age_payee(migrated_engine, payee["id"], 48)
    alice.extra["payee_id"] = uuid.UUID(str(payee["id"]))
    return alice


def _order(
    engine: Engine,
    alice: Actor,
    status: ScheduledTransferStatus = ScheduledTransferStatus.PAUSED,
    reason: SchedulePauseReason | None = SchedulePauseReason.FRAUD_BLOCK,
) -> uuid.UUID:
    tomorrow = utcnow().date() + timedelta(days=1)
    with Session(engine) as session:
        order = ScheduledTransfer(
            customer_id=alice.customer_id,
            user_id=alice.user_id,
            from_account_id=alice.account_id,
            payee_id=alice.extra["payee_id"],
            amount=AMOUNT,
            currency="INR",
            frequency=ScheduleFrequency.MONTHLY,
            status=status,
            pause_reason=reason,
            start_date=tomorrow,
            next_run_date=tomorrow,
        )
        session.add(order)
        session.commit()
        return order.id


def _audit_rows(engine: Engine, order_id: uuid.UUID) -> list[AuditLog]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(AuditLog).where(
                    AuditLog.action == ACTION, AuditLog.resource_id == str(order_id)
                )
            ).scalars()
        )


def _unlock_events(engine: Engine) -> list[OutboxEvent]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "SCHEDULED_TRANSFER_UNLOCKED")
            ).scalars()
        )


def _state(engine: Engine, order_id: uuid.UUID) -> tuple[str, str | None]:
    with Session(engine) as session:
        order = session.get(ScheduledTransfer, order_id)
        assert order is not None
        return order.status.value, order.pause_reason


@pytest.mark.parametrize("role", [RoleName.FRAUD_ANALYST, RoleName.ADMIN])
def test_analyst_clears_the_lock_and_the_customer_resumes(
    client: TestClient,
    alice: Actor,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    role: RoleName,
) -> None:
    order_id = _order(migrated_engine, alice)
    analyst = make_staff(role)

    res = client.post(_url(order_id), headers=analyst.headers, json=REASON)

    assert res.status_code == 200, res.text
    assert res.json() == {"id": str(order_id), "status": "PAUSED", "pause_reason": "CUSTOMER"}
    # never restarts unattended: still paused, only the reason changed
    assert _state(migrated_engine, order_id) == ("PAUSED", "CUSTOMER")

    [audit] = _audit_rows(migrated_engine, order_id)
    assert audit.actor_user_id == analyst.user_id
    assert audit.details["reason"] == REASON["reason"]
    assert audit.details["customer_user_id"] == str(alice.user_id)

    with Session(migrated_engine) as session:
        relay_outbox(session)
        session.commit()
        [bell] = session.execute(
            select(Notification).where(
                Notification.user_id == alice.user_id,
                Notification.type == "SCHEDULED_TRANSFER_UNLOCKED",
            )
        ).scalars()
        assert bell.resource_id == order_id
        text = f"{bell.title} {bell.body}".lower()
        # no score, decision reasons or analyst note reach the customer
        for leaked in ("risk", "score", "fraud", "phone", "reason"):
            assert leaked not in text, leaked

    resumed = client.post(f"/api/v1/transfers/scheduled/{order_id}/resume", headers=alice.headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ACTIVE"


def test_clearing_twice_is_a_conflict_and_records_nothing_more(
    client: TestClient, alice: Actor, make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:
    order_id = _order(migrated_engine, alice)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.post(_url(order_id), headers=analyst.headers, json=REASON).status_code == 200

    again = client.post(_url(order_id), headers=analyst.headers, json=REASON)

    assert again.status_code == 409
    assert len(_audit_rows(migrated_engine, order_id)) == 1
    assert len(_unlock_events(migrated_engine)) == 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (ScheduledTransferStatus.ACTIVE, None),
        (ScheduledTransferStatus.PAUSED, SchedulePauseReason.CUSTOMER),
        (ScheduledTransferStatus.PAUSED, SchedulePauseReason.CUSTOMER_ACTION_REQUIRED),
        (ScheduledTransferStatus.CANCELLED, None),
    ],
)
def test_an_order_not_paused_by_fraud_is_a_conflict(
    client: TestClient,
    alice: Actor,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    status: ScheduledTransferStatus,
    reason: SchedulePauseReason | None,
) -> None:
    order_id = _order(migrated_engine, alice, status, reason)
    analyst = make_staff(RoleName.FRAUD_ANALYST)

    res = client.post(_url(order_id), headers=analyst.headers, json=REASON)

    assert res.status_code == 409
    assert _state(migrated_engine, order_id) == (status.value, reason.value if reason else None)
    assert _audit_rows(migrated_engine, order_id) == []
    assert _unlock_events(migrated_engine) == []


def test_an_unknown_order_is_not_found(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    res = client.post(_url(uuid.uuid4()), headers=analyst.headers, json=REASON)
    assert res.status_code == 404


def test_customers_and_other_staff_cannot_clear(
    client: TestClient, alice: Actor, make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:
    order_id = _order(migrated_engine, alice)
    # the order's own customer may not unlock it either
    callers = [
        alice,
        make_staff(RoleName.RELATIONSHIP_MANAGER),
        make_staff(RoleName.COMPLIANCE_OFFICER),
    ]
    for caller in callers:
        assert client.post(_url(order_id), headers=caller.headers, json=REASON).status_code == 403
    no_mfa = make_staff(RoleName.FRAUD_ANALYST, mfa=False)
    denied = client.post(_url(order_id), headers=no_mfa.headers, json=REASON)
    assert denied.status_code == 403 and denied.json()["code"] == "mfa_required"
    assert client.post(_url(order_id), json=REASON).status_code == 401

    assert _state(migrated_engine, order_id) == ("PAUSED", "FRAUD_BLOCK")
    assert _audit_rows(migrated_engine, order_id) == []


@pytest.mark.parametrize(
    "body", [{}, {"reason": ""}, {"reason": "  ok  "}, {"reason": "x" * 501}, {**REASON, "x": 1}]
)
def test_a_bounded_reason_is_required(
    client: TestClient,
    alice: Actor,
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    body: dict[str, Any],
) -> None:
    order_id = _order(migrated_engine, alice)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.post(_url(order_id), headers=analyst.headers, json=body).status_code == 422
    assert _state(migrated_engine, order_id) == ("PAUSED", "FRAUD_BLOCK")


def test_concurrent_clears_unlock_once(
    client: TestClient, alice: Actor, make_staff: Callable[..., Actor], migrated_engine: Engine
) -> None:
    order_id = _order(migrated_engine, alice)
    analysts = [make_staff(RoleName.FRAUD_ANALYST) for _ in range(4)]

    def clear(analyst: Actor) -> int:
        return client.post(_url(order_id), headers=analyst.headers, json=REASON).status_code

    with ThreadPoolExecutor(max_workers=len(analysts)) as pool:
        codes = sorted(pool.map(clear, analysts))

    assert codes == [200, 409, 409, 409]
    assert _state(migrated_engine, order_id) == ("PAUSED", "CUSTOMER")
    assert len(_audit_rows(migrated_engine, order_id)) == 1
    assert len(_unlock_events(migrated_engine)) == 1
