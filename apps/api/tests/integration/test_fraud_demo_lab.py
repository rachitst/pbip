"""TASK-081: the staff fraud demonstration lab's two server hooks.

The lab drives the real customer transfer API; the server only (1) says whether the lab may run
in this environment and (2) lets the lab find the alert raised for the transfer it just sent.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings, get_settings
from pbip_api.modules.auth.models import RoleName
from pbip_test_support import Actor

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


def test_lab_is_enabled_outside_production(
    client: TestClient, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    response = client.get(LAB, headers=analyst.headers)
    assert response.status_code == 200 and response.json() == {"enabled": True}


def test_lab_is_disabled_when_app_env_is_production(
    app: FastAPI, client: TestClient, settings: Settings, make_staff: Callable[..., Actor]
) -> None:
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    production = settings.model_copy(update={"app_env": "production"})
    app.dependency_overrides[get_settings] = lambda: production
    response = client.get(LAB, headers=analyst.headers)
    assert response.status_code == 200 and response.json() == {"enabled": False}


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
