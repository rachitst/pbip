"""TASK-081: the staff fraud demonstration lab's two server hooks.

The lab drives the real customer transfer API; the server only (1) says whether the lab may run
in this environment and (2) lets the lab find the alert raised for the transfer it just sent.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.core.db import get_session
from pbip_api.core.redis import get_redis
from pbip_api.modules.accounts.models import Account
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.ledger.models import EntryDirection, TransactionEntry
from pbip_api.modules.ledger.service import entry_balance
from pbip_ml.fraud.contract import FraudScorer, FraudScoreResult, TransactionContext
from pbip_test_support import PASSWORD, Actor
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration

LAB = "/api/v1/fraud/demo-lab"


def _transfer(
    client: TestClient, sender: Actor, payee: dict[str, Any], amount: int, key: str
) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**sender.headers, "Idempotency-Key": key},
        json={
            "from_account_id": str(sender.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
        },
    )


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_lab_is_enabled_only_in_local_and_test(
    app_env: str,
    app: FastAPI,
    client: TestClient,
    settings: Settings,
    make_staff: Callable[..., Actor],
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    demo = settings.model_copy(update={"app_env": app_env})
    app.dependency_overrides[get_settings] = lambda: demo
    response = client.get(LAB, headers=analyst.headers)
    assert response.status_code == 200 and response.json() == {"enabled": True}


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_lab_does_not_exist_in_staging_or_production(
    app_env: str,
    app: FastAPI,
    client: TestClient,
    settings: Settings,
    make_staff: Callable[..., Actor],
) -> None:
    """A deployed environment answers 404 even to an MFA-verified analyst or admin."""
    deployed = settings.model_copy(update={"app_env": app_env})
    app.dependency_overrides[get_settings] = lambda: deployed
    for role in (RoleName.FRAUD_ANALYST, RoleName.ADMIN):
        response = client.get(LAB, headers=make_staff(role).headers)
        assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_lab_status_is_staff_only_with_mfa(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    assert client.get(LAB).status_code == 401
    assert client.get(LAB, headers=make_customer().headers).status_code == 403
    assert (
        client.get(LAB, headers=make_staff(RoleName.COMPLIANCE_OFFICER).headers).status_code == 403
    )
    no_mfa = client.get(LAB, headers=make_staff(RoleName.FRAUD_ANALYST, mfa=False).headers)
    assert no_mfa.status_code == 403 and no_mfa.json()["code"] == "mfa_required"


def test_alert_queue_filters_by_transaction_id(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    make_staff: Callable[..., Actor],
) -> None:
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)  # minutes old: >= 25,000.00 is held for review
    first = _transfer(client, alice, payee, 3_000_000, "lab-filter-first")
    second = _transfer(client, alice, payee, 3_100_000, "lab-filter-second")
    assert first.json()["status"] == second.json()["status"] == "PENDING_REVIEW"
    analyst = make_staff(RoleName.FRAUD_ANALYST)

    assert len(client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()) == 2
    only = client.get(
        f"/api/v1/fraud/alerts?transaction_id={first.json()['id']}", headers=analyst.headers
    ).json()
    assert len(only) == 1 and only[0]["amount_minor"] == 3_000_000
    detail = client.get(f"/api/v1/fraud/alerts/{only[0]['id']}", headers=analyst.headers).json()
    assert detail["transaction_id"] == first.json()["id"]


def test_alert_queue_filter_for_an_unflagged_transaction_is_empty(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    unknown = "00000000-0000-0000-0000-000000000081"
    response = client.get(f"/api/v1/fraud/alerts?transaction_id={unknown}", headers=analyst.headers)
    assert response.status_code == 200 and response.json() == []
    bad = client.get("/api/v1/fraud/alerts?transaction_id=not-a-uuid", headers=analyst.headers)
    assert bad.status_code == 422


class _SpyScorer:
    """Delegates to the scorer the payments path built, recording what it was asked and said."""

    def __init__(self, inner: FraudScorer, seen: list[tuple[TransactionContext, Any]]) -> None:
        self.inner, self.seen = inner, seen

    def score(self, context: TransactionContext) -> FraudScoreResult:
        result = self.inner.score(context)
        self.seen.append((context, result))
        return result


def test_lab_transfers_use_the_payment_scorer_and_move_money_only_through_the_ledger(
    app: FastAPI,
    client: TestClient,
    db: Session,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    """Replays the lab's F1 call sequence (FraudDemoLab.test.tsx pins it) against the real API.

    The lab has no transfer path of its own: every transfer is the customer's POST /transfers,
    scored once by the same ``get_fraud_scorer`` seam, and the score the lab shows is the one that
    scorer returned. Money moves only by ledger holds/entries on the demo customer's account; a
    bystander customer's balance is untouched.
    """
    seen: list[tuple[TransactionContext, Any]] = []

    def spying(
        request: Request,
        session: Session = Depends(get_session),
        settings: Settings = Depends(get_settings),
        redis_client: Redis = Depends(get_redis),
    ) -> FraudScorer:
        return _SpyScorer(get_fraud_scorer(request, session, settings, redis_client), seen)

    app.dependency_overrides[get_fraud_scorer] = spying
    victim, bystander = make_customer(funds=10_000_000), make_customer(funds=5_000_000)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    token = client.post(
        "/api/v1/auth/login", json={"email": victim.email, "password": PASSWORD}
    ).json()["access_token"]
    customer = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/payees",
        headers=customer,
        json={
            "name": "Urgent Investments",
            "account_number": "SCAM0081LAB1",
            "bank_code": "EXTB0001",
        },
    )
    assert created.status_code == 201, created.text
    verified = client.post(f"/api/v1/payees/{created.json()['id']}/verify", headers=customer)
    payee = verified.json()["payee"]
    assert payee["status"] == "VERIFIED"

    signed_in = replace(victim, access_token=token)  # the lab's own sign-in, not the fixture's
    small = _transfer(client, signed_in, payee, 50_000, "lab-small")
    large = _transfer(client, signed_in, payee, 3_000_000, "lab-large")
    assert small.status_code == 201 and small.json()["status"] == "POSTED"
    assert large.status_code == 202 and large.json()["status"] == "PENDING_REVIEW"

    # Scored exactly once per transfer, by the payment path's scorer, for that transaction.
    assert [str(ctx.transaction_id) for ctx, _ in seen] == [small.json()["id"], large.json()["id"]]
    held_result = seen[1][1]
    shown = client.get(
        f"/api/v1/fraud/alerts?transaction_id={large.json()['id']}", headers=analyst.headers
    ).json()
    detail = client.get(f"/api/v1/fraud/alerts/{shown[0]['id']}", headers=analyst.headers).json()
    assert detail["risk_score"] == held_result.risk_score
    assert detail["decision"] == held_result.decision.value
    assert detail["model_version"] == held_result.model_version
    assert [r["code"] for r in detail["reasons"]] == [r.code for r in held_result.reasons]

    # Ledger: entries balance, materialised balances reconcile, the hold covers the held transfer.
    db.expire_all()
    for tx_id in (small.json()["id"], large.json()["id"]):
        signed = db.execute(
            select(TransactionEntry.direction, TransactionEntry.amount).where(
                TransactionEntry.transaction_id == UUID(tx_id)
            )
        ).all()
        assert sum(a if d == EntryDirection.CREDIT else -a for d, a in signed) == 0
    victim_account = db.get(Account, victim.account_id)
    bystander_account = db.get(Account, bystander.account_id)
    assert victim_account is not None and bystander_account is not None
    assert victim_account.ledger_balance == 10_000_000 - 50_000
    assert victim_account.held_amount == 3_000_000
    assert victim_account.ledger_balance == entry_balance(db, victim_account.id)
    assert bystander_account.ledger_balance == 5_000_000
    assert bystander_account.held_amount == 0
    assert bystander_account.ledger_balance == entry_balance(db, bystander_account.id)
