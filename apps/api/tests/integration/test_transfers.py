"""The transfer flow end to end: validation, idempotency, limits, fraud decisions, step-up, review."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.core.metrics import TRANSFERS_TOTAL
from pbip_api.modules.accounts.models import AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.compliance.models import AuditLog
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.fraud.models import AlertStatus, FraudAlert, FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import IdempotencyKey, Transaction
from pbip_api.modules.notifications.models import OutboxEvent
from pbip_api.modules.payments.service import PaymentsService
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, FakeClock, age_payee, totp_code
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def new_key() -> dict[str, str]:
    return {"Idempotency-Key": f"key-{uuid.uuid4().hex}"}


@pytest.fixture
def pair(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    """Alice (funded, verified payee for Bob who is 48h old, so baseline fraud rules stay quiet)."""
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, bob, payee


def send(
    client: TestClient,
    alice: Actor,
    payee: dict[str, Any],
    amount: int = 10_000,
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **(headers or new_key())},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": amount,
            "payee_id": payee["id"],
            "reference": "rent",
        },
    )


def balance(client: TestClient, actor: Actor) -> dict[str, int]:
    body = client.get(f"/api/v1/accounts/{actor.account_id}", headers=actor.headers).json()
    return {
        "ledger": body["ledger_balance_minor"],
        "held": body["held_minor"],
        "available": body["available_minor"],
    }


def force_decision(app: FastAPI, decision: FraudDecision) -> None:
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


# --------------------------------------------------------------------------- happy path
def test_transfer_moves_money_and_writes_balanced_entries(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, bob, payee = pair
    response = send(client, alice, payee, 25_000)
    assert response.status_code == 201 and response.json()["status"] == "POSTED"
    assert balance(client, alice)["ledger"] == 10_000_000 - 25_000
    assert balance(client, bob)["ledger"] == 25_000
    entries = client.get(
        f"/api/v1/accounts/{alice.account_id}/transactions", headers=alice.headers
    ).json()["items"]
    assert (
        entries[0]["direction"] == "DEBIT"
        and entries[0]["amount_minor"] == 25_000
        and entries[0]["balance_after_minor"] == 10_000_000 - 25_000
    )
    fetched = client.get(f"/api/v1/transfers/{response.json()['id']}", headers=alice.headers)
    assert fetched.status_code == 200 and fetched.json()["status"] == "POSTED"


def test_transfer_between_own_accounts(
    client: TestClient, make_customer: Callable[..., Actor], migrated_engine: Engine
) -> None:
    alice = make_customer(funds=50_000)
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
        second = open_account(session, alice.customer_id, AccountType.CURRENT)  # type: ignore[arg-type]
        session.commit()
    response = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 20_000,
            "to_account_id": str(second.id),
        },
    )
    assert response.status_code == 201
    same = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **new_key()},
        json={
            "from_account_id": str(alice.account_id),
            "amount_minor": 1,
            "to_account_id": str(alice.account_id),
        },
    )
    assert same.status_code == 422


# --------------------------------------------------------------------------- idempotency
def test_retry_with_same_key_returns_the_original_result_without_a_duplicate(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, bob, payee = pair
    key = new_key()
    first = send(client, alice, payee, 40_000, key)
    second = send(client, alice, payee, 40_000, key)
    third = send(client, alice, payee, 40_000, key)
    assert first.status_code == second.status_code == third.status_code == 201
    assert first.json() == second.json() == third.json()
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert balance(client, alice)["ledger"] == 10_000_000 - 40_000
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM transactions WHERE type='TRANSFER'")
            ).scalar_one()
            == 1
        )


def test_same_key_with_a_different_request_is_rejected(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _bob, payee = pair
    key = new_key()
    assert send(client, alice, payee, 1_000, key).status_code == 201
    clash = send(client, alice, payee, 2_000, key)
    assert clash.status_code == 422 and clash.json()["code"] == "idempotency_key_reuse"


def test_concurrent_duplicates_create_exactly_one_transfer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, _bob, payee = pair
    key = new_key()

    def fire(_: int) -> Any:
        return send(TestClient(app), alice, payee, 5_000, key)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(fire, range(10)))
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum("idempotent-replayed" not in r.headers for r in responses) == 1  # one real execution
    assert balance(client, alice)["ledger"] == 10_000_000 - 5_000
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM transactions WHERE type='TRANSFER'")
            ).scalar_one()
            == 1
        )


def test_failed_request_is_not_cached_so_the_client_can_retry_with_the_same_key(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> None:
    alice, bob = make_customer(funds=1_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    key = new_key()
    broke = send(client, alice, payee, 5_000, key)
    assert broke.status_code == 422 and broke.json()["code"] == "insufficient_funds"
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM transactions WHERE type='TRANSFER'")
            ).scalar_one()
            == 0
        )
        assert connection.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0
    with migrated_engine.begin() as connection:  # simulate a top-up
        connection.execute(
            text("UPDATE accounts SET ledger_balance = 100000 WHERE id = :a"),
            {"a": alice.account_id},
        )
    assert send(client, alice, payee, 5_000, key).status_code == 201


# --------------------------------------------------------------------------- validation + limits
def test_input_validation(client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]]) -> None:
    alice, _bob, payee = pair
    base = {"from_account_id": str(alice.account_id), "payee_id": payee["id"]}
    h = {**alice.headers, **new_key()}
    assert (
        client.post(
            "/api/v1/transfers", headers=alice.headers, json={**base, "amount_minor": 100}
        ).status_code
        == 422
    )  # no key
    assert (
        client.post(
            "/api/v1/transfers",
            headers={**alice.headers, "Idempotency-Key": "short"},
            json={**base, "amount_minor": 100},
        ).status_code
        == 422
    )
    for bad in (0, -5, 1.5, "ten"):
        assert (
            client.post(
                "/api/v1/transfers",
                headers={**alice.headers, **new_key()},
                json={**base, "amount_minor": bad},
            ).status_code
            == 422
        )
    neither = client.post(
        "/api/v1/transfers",
        headers=h,
        json={"from_account_id": str(alice.account_id), "amount_minor": 100},
    )
    assert neither.status_code == 422
    wrong_currency = client.post(
        "/api/v1/transfers",
        headers={**alice.headers, **new_key()},
        json={**base, "amount_minor": 100, "currency": "USD"},
    )
    assert wrong_currency.status_code == 422


def test_unverified_payee_cannot_receive_money(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(funds=10_000), make_customer()
    created = client.post(
        "/api/v1/payees",
        headers=alice.headers,
        json={"name": "Test Customer", "account_number": bob.account_number},
    )
    response = send(client, alice, created.json(), 100)
    assert response.status_code == 422


def test_payee_name_mismatch_is_not_verified_and_never_reveals_the_real_name(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice, bob = make_customer(), make_customer()
    created = client.post(
        "/api/v1/payees",
        headers=alice.headers,
        json={"name": "Totally Different Person", "account_number": bob.account_number},
    ).json()
    result = client.post(f"/api/v1/payees/{created['id']}/verify", headers=alice.headers)
    assert (
        result.json()["name_match"] == "NO_MATCH"
        and result.json()["payee"]["status"] == "PENDING_VERIFICATION"
    )
    assert "Test Customer" not in result.text


def test_per_transfer_and_daily_limits(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], settings: Settings
) -> None:
    alice, _bob, payee = pair
    settings.max_transfer_minor = 100_000
    settings.daily_transfer_limit_minor = 150_000
    too_big = send(client, alice, payee, 100_001)
    assert too_big.status_code == 422 and too_big.json()["code"] == "limit_exceeded"
    assert send(client, alice, payee, 100_000).status_code == 201
    over_daily = send(client, alice, payee, 60_000)
    assert over_daily.status_code == 422 and "daily" in over_daily.json()["title"]
    assert send(client, alice, payee, 50_000).status_code == 201


# --------------------------------------------------------------------------- fraud decisions
def test_block_declines_without_moving_money_and_hides_the_reasons(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.BLOCK)
    response = send(client, alice, payee, 10_000)
    assert response.status_code == 403 and response.json()["status"] == "DECLINED"
    assert "reasons" not in response.text and "risk" not in response.text
    assert balance(client, alice)["ledger"] == 10_000_000
    # TASK-086: the stored transaction says WHY it was declined (analyst and audit evidence).
    with sessionmaker(bind=migrated_engine)() as session:
        declined = session.get(Transaction, uuid.UUID(response.json()["id"]))
        assert declined is not None and declined.decline_reason == "FRAUD_BLOCK"
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alerts = client.get("/api/v1/fraud/alerts?status=OPEN", headers=analyst.headers).json()
    assert (
        len(alerts) == 1
        and alerts[0]["severity"] == "HIGH"
        and alerts[0]["transaction_status"] == "DECLINED"
    )


def test_review_holds_funds_and_analyst_release_posts_the_transfer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
) -> None:
    alice, bob, payee = pair
    force_decision(app, FraudDecision.REVIEW)
    response = send(client, alice, payee, 300_000)
    assert response.status_code == 202 and response.json()["status"] == "PENDING_REVIEW"
    assert balance(client, alice) == {"ledger": 10_000_000, "held": 300_000, "available": 9_700_000}
    assert balance(client, bob)["ledger"] == 0

    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]
    detail = client.get(f"/api/v1/fraud/alerts/{alert['id']}", headers=analyst.headers).json()
    assert (
        detail["risk_score"] == 72
        and detail["model_version"] == "test-double-fixed"
        and detail["reasons"]
    )

    released = client.post(
        f"/api/v1/fraud/alerts/{alert['id']}/decision",
        headers=analyst.headers,
        json={"action": "RELEASE", "note": "customer confirmed by phone"},
    )
    assert released.status_code == 204
    assert balance(client, alice) == {"ledger": 9_700_000, "held": 0, "available": 9_700_000}
    assert balance(client, bob)["ledger"] == 300_000
    assert (
        client.get(f"/api/v1/transfers/{response.json()['id']}", headers=alice.headers).json()[
            "status"
        ]
        == "POSTED"
    )
    again = client.post(
        f"/api/v1/fraud/alerts/{alert['id']}/decision",
        headers=analyst.headers,
        json={"action": "REJECT"},
    )
    assert again.status_code == 409


def test_analyst_reject_releases_the_hold_and_declines(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
) -> None:
    alice, bob, payee = pair
    force_decision(app, FraudDecision.REVIEW)
    response = send(client, alice, payee, 300_000)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]
    assert (
        client.post(
            f"/api/v1/fraud/alerts/{alert['id']}/decision",
            headers=analyst.headers,
            json={"action": "REJECT"},
        ).status_code
        == 204
    )
    assert balance(client, alice) == {"ledger": 10_000_000, "held": 0, "available": 10_000_000}
    assert balance(client, bob)["ledger"] == 0
    assert (
        client.get(f"/api/v1/transfers/{response.json()['id']}", headers=alice.headers).json()[
            "status"
        ]
        == "DECLINED"
    )
    resolved = client.get(
        "/api/v1/fraud/alerts?status=CONFIRMED_FRAUD", headers=analyst.headers
    ).json()
    assert len(resolved) == 1


def test_step_up_with_mfa_completes_after_a_valid_code(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    pending = send(client, alice, payee, 50_000)
    assert pending.status_code == 202 and pending.json()["step_up_required"] is True
    assert balance(client, alice)["ledger"] == 10_000_000  # nothing moved yet
    tid = pending.json()["id"]

    wrong = client.post(
        f"/api/v1/transfers/{tid}/step-up", headers=alice.headers, json={"code": "000000"}
    )
    assert wrong.status_code == 422
    done = client.post(
        f"/api/v1/transfers/{tid}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert done.status_code == 200 and done.json()["status"] == "POSTED"
    assert balance(client, bob)["ledger"] == 50_000
    with sessionmaker(bind=migrated_engine)() as session:  # TASK-086: audit row names the transfer
        confirmed = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_STEP_UP_CONFIRMED")
        ).scalar_one()
        assert confirmed.resource_id == tid and confirmed.actor_user_id == alice.user_id
    replay = client.post(
        f"/api/v1/transfers/{tid}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert replay.status_code == 409


def test_step_up_without_mfa_escalates_to_analyst_review(
    app: FastAPI, client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.STEP_UP)
    response = send(client, alice, payee, 50_000)
    assert response.json()["status"] == "PENDING_REVIEW"
    assert balance(client, alice)["held"] == 50_000


# --------------------------------------------------------------------------- real baseline rules
def test_new_payee_plus_large_amount_is_held_with_human_readable_reasons(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    make_staff: Callable[..., Actor],
) -> None:
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)  # NOT aged: payee is minutes old
    response = send(client, alice, payee, 3_000_000)
    assert response.status_code == 202 and response.json()["status"] == "PENDING_REVIEW"
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]
    detail = client.get(f"/api/v1/fraud/alerts/{alert['id']}", headers=analyst.headers).json()
    codes = {r["code"] for r in detail["reasons"]}
    assert "NEW_PAYEE_LARGE_AMOUNT" in codes
    # TASK-036: the analyst sees who and what, without contact details.
    assert detail["customer"]["kyc_status"] and detail["customer"]["full_name"]
    assert set(detail["customer"]) == {
        "full_name",
        "kyc_status",
        "segment",
        "risk_profile",
        "customer_since",
    }
    assert detail["transaction_type"] == "TRANSFER" and detail["reference"] == "rent"
    assert (
        detail["model_version"] == "baseline-rules-0.3"
        and detail["features"]["payee_age_minutes"] is not None
    )
    assert any("minutes ago" in r["message"] for r in detail["reasons"])


def test_velocity_burst_is_flagged(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _bob, payee = pair
    statuses = [send(client, alice, payee, 100).json()["status"] for _ in range(4)]
    assert statuses[:3] == ["POSTED"] * 3
    assert statuses[3] == "PENDING_REVIEW"  # 4th transfer in 10 minutes, no MFA -> review


def test_amount_spike_against_history_is_flagged(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    migrated_engine: Engine,
    make_staff: Callable[..., Actor],
) -> None:
    alice, _bob, payee = pair
    for _ in range(3):
        assert send(client, alice, payee, 1_000).status_code == 201
    with migrated_engine.begin() as connection:  # spread history out so velocity does not fire
        connection.execute(text("UPDATE transactions SET created_at = now() - interval '2 hours'"))
    big = send(
        client, alice, payee, 8_000
    )  # 8x the usual 1,000 -> +35 -> STEP_UP -> no MFA -> REVIEW
    assert big.json()["status"] == "PENDING_REVIEW"
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]
    reasons = client.get(f"/api/v1/fraud/alerts/{alert['id']}", headers=analyst.headers).json()[
        "reasons"
    ]
    assert any(r["code"] == "AMOUNT_SPIKE" and "8.0x" in r["message"] for r in reasons)


def test_every_scoring_decision_is_persisted_for_audit_and_retraining(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, _bob, payee = pair
    send(client, alice, payee, 100)
    with migrated_engine.connect() as connection:
        row = connection.execute(
            text("SELECT decision, risk_score, model_version, latency_ms FROM fraud_events")
        ).one()
    assert (
        row.decision == "ALLOW"
        and row.model_version == "baseline-rules-0.3"
        and row.latency_ms >= 0
    )
    _ = (Transaction, func, select)  # imported for type narrowing in other tests


class _CountrySequence:
    """Test double: answers the next country in the list for every lookup."""

    def __init__(self, countries: list[str | None]) -> None:
        self.countries = countries

    def country(self, ip: str | None) -> str | None:
        return self.countries.pop(0)


def test_transfer_from_a_new_country_adds_a_new_country_reason(
    app: FastAPI,
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
    db: Session,
) -> None:
    """ADR-0014: the first located transfer sets a baseline, a repeat country is quiet, a different
    country adds NEW_COUNTRY (a signal, below the step-up threshold on its own), unknown adds nothing.
    The customer-facing response never shows the reason."""
    from pbip_api.modules.fraud.dependencies import get_country_resolver

    alice = make_customer(funds=1_000_000)
    bob = make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    countries = _CountrySequence(["IN", "IN", "GB", None])  # one instance across requests
    app.dependency_overrides[get_country_resolver] = lambda: countries

    # Two source accounts so the per-account velocity rule (4 in 10 minutes) stays out of the way.
    second = client.post(
        "/api/v1/accounts", headers=alice.headers, json={"account_type": "CURRENT"}
    )
    assert second.status_code == 201, second.text
    ledger.deposit(db, uuid.UUID(second.json()["id"]), 1_000_000)
    db.commit()
    sources = [alice.account_id, alice.account_id, second.json()["id"], second.json()["id"]]
    ids = []
    for source in sources:
        resp = client.post(
            "/api/v1/transfers",
            headers={**alice.headers, "Idempotency-Key": uuid.uuid4().hex},
            json={
                "from_account_id": str(source),
                "amount_minor": 1_000,
                "payee_id": payee["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert "reasons" not in resp.json() and "risk_score" not in resp.json()
        ids.append(uuid.UUID(resp.json()["id"]))

    events = {
        e.transaction_id: e
        for e in db.execute(select(FraudEvent).where(FraudEvent.transaction_id.in_(ids))).scalars()
    }
    codes = [[r["code"] for r in events[i].reasons] for i in ids]
    assert [events[i].features["ip_country"] for i in ids] == ["IN", "IN", "GB", None]
    assert "NEW_COUNTRY" not in codes[0]  # no baseline yet
    assert "NEW_COUNTRY" not in codes[1]  # same country
    assert "NEW_COUNTRY" in codes[2] and events[ids[2]].decision == "ALLOW"
    assert events[ids[2]].risk_score == 20
    assert "NEW_COUNTRY" not in codes[3]  # unknown country never adds risk


def test_three_wrong_step_up_codes_hand_the_transfer_to_an_analyst(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    make_staff: Callable[..., Actor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-037: after 3 failed codes the transfer is held for review, not left retryable."""
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 50_000).json()["id"]
    url = f"/api/v1/transfers/{tid}/step-up"
    for _ in range(2):
        wrong = client.post(url, headers=alice.headers, json={"code": "000000"})
        assert wrong.status_code == 422
    locked: list[object] = []
    real_lock = PaymentsService._lock_user

    def spy(self: PaymentsService, user_id: uuid.UUID) -> None:
        locked.append(user_id)
        real_lock(self, user_id)

    monkeypatch.setattr(PaymentsService, "_lock_user", spy)
    third = client.post(url, headers=alice.headers, json={"code": "000000"})
    assert third.status_code == 200 and third.json()["status"] == "PENDING_REVIEW"
    # TASK-086: the escalation takes THIS user's advisory lock (AGENTS lock order), never a shared key.
    assert locked and set(locked) == {alice.user_id}
    with sessionmaker(bind=migrated_engine)() as session:
        escalated = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_STEP_UP_ESCALATED")
        ).scalar_one()
        assert escalated.resource_id == tid and escalated.ip == "testclient"
    assert balance(client, alice)["held"] == 50_000  # money held for the analyst
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alerts = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()
    assert [a["transaction_status"] for a in alerts] == ["PENDING_REVIEW"]
    again = client.post(url, headers=alice.headers, json={"code": "000000"})
    assert again.status_code == 409  # no longer waiting for a code


def test_a_correct_code_after_two_wrong_ones_still_completes(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 50_000).json()["id"]
    url = f"/api/v1/transfers/{tid}/step-up"
    for _ in range(2):
        assert client.post(url, headers=alice.headers, json={"code": "000000"}).status_code == 422
    done = client.post(url, headers=alice.headers, json={"code": totp_code(secret, clock)})
    assert done.status_code == 200 and done.json()["status"] == "POSTED"
    assert balance(client, bob)["ledger"] == 50_000


# --------------------------------------------------------------- TASK-086 mutation gaps
# Tests added for mutants that survived the mutation run (docs/testing/mutation-report.md): they pin
# down what an analyst decision and the daily limit must leave behind, not only the HTTP status.


def _decide(
    client: TestClient, analyst: Actor, alert_id: str, action: str, note: str | None
) -> Any:
    body: dict[str, Any] = {"action": action}
    if note is not None:
        body["note"] = note
    return client.post(
        f"/api/v1/fraud/alerts/{alert_id}/decision", headers=analyst.headers, json=body
    )


@pytest.mark.parametrize("release", [True, False])
def test_analyst_decision_records_who_why_and_tells_the_customer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
    release: bool,
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.REVIEW)
    tid = send(client, alice, payee, 300_000).json()["id"]
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert_id = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]["id"]
    action, note = ("RELEASE", "customer confirmed by phone") if release else ("REJECT", "mule")
    assert _decide(client, analyst, alert_id, action, note).status_code == 204

    with sessionmaker(bind=migrated_engine)() as session:
        alert = session.get(FraudAlert, uuid.UUID(alert_id))
        assert alert is not None
        assert alert.status == (
            AlertStatus.FALSE_POSITIVE if release else AlertStatus.CONFIRMED_FRAUD
        )
        assert alert.resolution_note == note and alert.assigned_to_user_id == analyst.user_id
        assert alert.resolved_at is not None
        txn = session.get(Transaction, uuid.UUID(tid))
        assert txn is not None
        if release:
            assert txn.status == "POSTED" and txn.decline_reason is None
        else:
            assert txn.status == "DECLINED" and txn.decline_reason == "ANALYST_REJECTED"
        told = (
            session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.payload["transaction_id"].astext == tid)
                .order_by(OutboxEvent.created_at.desc())
            )
            .scalars()
            .first()
        )
        assert told is not None and told.payload["user_id"] == str(alice.user_id)
        assert told.event_type == ("TRANSFER_POSTED" if release else "TRANSFER_DECLINED")
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "FRAUD_ALERT_RESOLVED")
        ).scalar_one()
        assert (audit.actor_user_id, audit.resource_type, audit.resource_id) == (
            analyst.user_id,
            "fraud_alert",
            alert_id,
        )
        assert audit.ip == "testclient" and audit.request_id
        assert audit.details == {"release": release, "transaction_id": tid}


def test_labelling_an_engine_block_keeps_it_declined_and_tells_the_customer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
    migrated_engine: Engine,
) -> None:
    """A BLOCK has no hold: even a RELEASE label must not post it (no money moves on a label)."""
    alice, bob, payee = pair
    force_decision(app, FraudDecision.BLOCK)
    tid = send(client, alice, payee, 10_000).json()["id"]
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alert_id = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]["id"]
    assert _decide(client, analyst, alert_id, "RELEASE", None).status_code == 204
    assert balance(client, alice)["ledger"] == 10_000_000 and balance(client, bob)["ledger"] == 0
    with sessionmaker(bind=migrated_engine)() as session:
        txn = session.get(Transaction, uuid.UUID(tid))
        assert txn is not None and txn.status == "DECLINED" and txn.decline_reason == "FRAUD_BLOCK"
        told = (
            session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.payload["transaction_id"].astext == tid)
                .order_by(OutboxEvent.created_at.desc())
            )
            .scalars()
            .first()
        )
        assert told is not None and told.event_type == "TRANSFER_DECLINED"


def test_decision_errors_are_specific(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.REVIEW)
    send(client, alice, payee, 300_000)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    missing = _decide(client, analyst, str(uuid.uuid4()), "REJECT", None)
    assert missing.status_code == 404 and missing.json()["title"] == "alert not found"
    alert_id = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()[0]["id"]
    assert _decide(client, analyst, alert_id, "REJECT", None).status_code == 204
    twice = _decide(client, analyst, alert_id, "RELEASE", None)
    assert twice.status_code == 409 and twice.json()["title"] == "alert already resolved"


_NOW = datetime(2026, 11, 2, 23, 59, 59, 999_999, tzinfo=UTC)


@pytest.mark.parametrize(
    ("earlier_at", "counted"),
    [
        (_NOW.replace(hour=0, minute=0, second=0, microsecond=0), True),  # exactly midnight UTC
        (
            _NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1),
            False,
        ),
    ],
)
def test_daily_limit_window_starts_exactly_at_midnight_utc(
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    earlier_at: datetime,
    counted: bool,
) -> None:
    alice, _bob, payee = pair
    settings.daily_transfer_limit_minor = 150_000
    earlier = send(client, alice, payee, 100_000)
    assert earlier.status_code == 201
    with migrated_engine.begin() as conn:  # backdate the header only; entries are untouched
        conn.execute(
            update(Transaction)
            .where(Transaction.id == uuid.UUID(earlier.json()["id"]))
            .values(created_at=earlier_at)
        )
    monkeypatch.setattr("pbip_api.modules.payments.service.utcnow", lambda: _NOW)
    second = send(client, alice, payee, 60_000)
    if counted:
        assert (
            second.status_code == 422 and second.json()["title"] == "daily transfer limit exceeded"
        )
    else:
        assert second.status_code == 201


def test_a_transfer_of_exactly_the_daily_limit_is_allowed(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], settings: Settings
) -> None:
    """The limit is inclusive, the transfer under check is not counted twice, and an empty day
    sums to zero."""
    alice, _bob, payee = pair
    settings.daily_transfer_limit_minor = 150_000
    assert send(client, alice, payee, 150_000).status_code == 201
    assert send(client, alice, payee, 1).status_code == 422


def _latest_event(engine: Engine, tid: str) -> OutboxEvent:
    with sessionmaker(bind=engine)() as session:
        event = (
            session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.payload["transaction_id"].astext == tid)
                .order_by(OutboxEvent.created_at.desc())
            )
            .scalars()
            .first()
        )
        assert event is not None
        return event


def test_step_up_is_scoped_to_the_owner_and_to_that_transfer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    """BOLA and wrong-row guards on _owned_transaction: another customer gets 404 (indistinguishable
    from missing), and confirming one pending transfer never touches the owner's other one."""
    alice, bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    first = send(client, alice, payee, 20_000).json()["id"]
    second = send(client, alice, payee, 30_000).json()["id"]
    stolen = client.post(
        f"/api/v1/transfers/{first}/step-up", headers=bob.headers, json={"code": "000000"}
    )
    assert stolen.status_code == 404 and stolen.json()["title"] == "transfer not found"
    done = client.post(
        f"/api/v1/transfers/{second}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert done.status_code == 200 and done.json()["id"] == second
    with sessionmaker(bind=migrated_engine)() as session:
        statuses = {
            str(t.id): t.status
            for t in session.execute(
                select(Transaction).where(Transaction.id.in_([uuid.UUID(first), uuid.UUID(second)]))
            ).scalars()
        }
    assert statuses == {first: "PENDING_STEP_UP", second: "POSTED"}
    assert balance(client, bob)["ledger"] == 30_000
    not_waiting = client.post(
        f"/api/v1/transfers/{second}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert not_waiting.status_code == 409
    assert not_waiting.json()["title"] == "this transfer is not waiting for verification"


def test_confirmed_step_up_is_audited_notified_and_counted(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, _bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 25_000).json()["id"]
    posted_before = TRANSFERS_TOTAL.labels("POSTED")._value.get()
    ok = client.post(
        f"/api/v1/transfers/{tid}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert ok.status_code == 200
    assert TRANSFERS_TOTAL.labels("POSTED")._value.get() == posted_before + 1
    with sessionmaker(bind=migrated_engine)() as session:
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_STEP_UP_CONFIRMED")
        ).scalar_one()
        assert audit.resource_type == "transaction" and audit.ip == "testclient"
        assert audit.request_id
    event = _latest_event(migrated_engine, tid)
    assert event.event_type == "TRANSFER_POSTED"
    assert event.payload == {
        "user_id": str(alice.user_id),
        "transaction_id": tid,
        "account_id": str(alice.account_id),
        "amount_minor": 25_000,
        "currency": "INR",
    }


def test_escalation_is_audited_notified_and_counted(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 40_000).json()["id"]
    review_before = TRANSFERS_TOTAL.labels("PENDING_REVIEW")._value.get()
    for _ in range(3):
        last = client.post(
            f"/api/v1/transfers/{tid}/step-up", headers=alice.headers, json={"code": "000000"}
        )
    assert last.status_code == 200 and last.json()["id"] == tid
    assert last.json()["status"] == "PENDING_REVIEW"
    assert TRANSFERS_TOTAL.labels("PENDING_REVIEW")._value.get() == review_before + 1
    with sessionmaker(bind=migrated_engine)() as session:
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "TRANSFER_STEP_UP_ESCALATED")
        ).scalar_one()
        assert audit.actor_user_id == alice.user_id and audit.resource_type == "transaction"
        assert audit.request_id and audit.details == {"failed_codes": 3}
    event = _latest_event(migrated_engine, tid)
    assert event.event_type == "TRANSFER_UNDER_REVIEW"
    assert event.payload["user_id"] == str(alice.user_id)


@pytest.mark.parametrize("path", ["confirm", "escalate"])
def test_daily_limit_is_rechecked_when_a_held_transfer_finally_moves(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    settings: Settings,
    path: str,
) -> None:
    """The limit is enforced against what the customer has sent TODAY when money moves or is held,
    not only when the transfer was first created (another transfer may have used it up since)."""
    alice, _bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 80_000).json()["id"]
    force_decision(app, FraudDecision.ALLOW)
    assert send(client, alice, payee, 80_000).status_code == 201  # uses the day's allowance
    settings.daily_transfer_limit_minor = 150_000
    url = f"/api/v1/transfers/{tid}/step-up"
    if path == "confirm":
        last = client.post(url, headers=alice.headers, json={"code": totp_code(secret, clock)})
    else:
        for _ in range(3):
            last = client.post(url, headers=alice.headers, json={"code": "000000"})
    assert last.status_code == 422 and last.json()["code"] == "limit_exceeded"


@pytest.mark.parametrize("path", ["confirm", "escalate"])
def test_a_held_transfer_within_the_limit_is_not_counted_twice_when_it_moves(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    settings: Settings,
    path: str,
) -> None:
    """The recheck excludes the transfer under check: 100k waiting for step-up is already 'live'
    today, and counting it again would refuse a transfer that is within the 150k limit."""
    alice, _bob, payee = pair
    secret = enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    settings.daily_transfer_limit_minor = 150_000
    force_decision(app, FraudDecision.STEP_UP)
    tid = send(client, alice, payee, 100_000).json()["id"]
    url = f"/api/v1/transfers/{tid}/step-up"
    if path == "confirm":
        last = client.post(url, headers=alice.headers, json={"code": totp_code(secret, clock)})
        assert last.status_code == 200 and last.json()["status"] == "POSTED"
    else:
        for _ in range(3):
            last = client.post(url, headers=alice.headers, json={"code": "000000"})
        assert last.status_code == 200 and last.json()["status"] == "PENDING_REVIEW"


def test_a_transfer_held_for_review_counts_the_days_earlier_spending(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
) -> None:
    alice, _bob, payee = pair
    settings.daily_transfer_limit_minor = 150_000
    assert send(client, alice, payee, 80_000).status_code == 201
    force_decision(app, FraudDecision.REVIEW)
    held = send(client, alice, payee, 80_000)
    assert held.status_code == 422 and held.json()["code"] == "limit_exceeded"
    assert balance(client, alice)["held"] == 0


def test_holds_allow_the_exact_balance_and_accumulate(
    app: FastAPI, client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]]
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.REVIEW)
    assert send(client, alice, payee, 4_000_000).status_code == 202
    assert send(client, alice, payee, 6_000_000).status_code == 202  # exactly what is left
    assert balance(client, alice) == {"ledger": 10_000_000, "held": 10_000_000, "available": 0}
    broke = send(client, alice, payee, 1)
    assert broke.status_code == 422 and broke.json()["title"] == "insufficient funds"


def test_the_idempotency_row_records_which_transfer_it_answered(
    client: TestClient, pair: tuple[Actor, Actor, dict[str, Any]], migrated_engine: Engine
) -> None:
    alice, _bob, payee = pair
    key = new_key()
    tid = send(client, alice, payee, 5_000, key).json()["id"]
    with sessionmaker(bind=migrated_engine)() as session:
        row = session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key["Idempotency-Key"])
        ).scalar_one()
        assert str(row.transaction_id) == tid and row.response_status == 201


def test_wrong_step_up_codes_count_per_transfer(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    """Two wrong codes on one transfer and two on another: neither has reached the limit of three."""
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    first = send(client, alice, payee, 10_000).json()["id"]
    second = send(client, alice, payee, 20_000).json()["id"]
    for tid in (first, second):
        for _ in range(2):
            wrong = client.post(
                f"/api/v1/transfers/{tid}/step-up", headers=alice.headers, json={"code": "000000"}
            )
            assert wrong.status_code == 422
    with sessionmaker(bind=migrated_engine)() as session:
        statuses = (
            session.execute(
                select(Transaction.status).where(
                    Transaction.id.in_([uuid.UUID(first), uuid.UUID(second)])
                )
            )
            .scalars()
            .all()
        )
    assert sorted(statuses) == ["PENDING_STEP_UP", "PENDING_STEP_UP"]


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        (FraudDecision.ALLOW, "Transfer completed"),
        (FraudDecision.STEP_UP, "Additional verification is required"),
        (FraudDecision.REVIEW, "Your transfer is being reviewed for your protection"),
        (FraudDecision.BLOCK, "We could not process this transfer. Please contact support"),
    ],
)
def test_customer_sees_the_exact_plain_message_for_each_outcome(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    decision: FraudDecision,
    message: str,
) -> None:
    """The customer-facing wording is a contract: plain, and never a fraud reason or score."""
    alice, _bob, payee = pair
    if decision == FraudDecision.STEP_UP:  # without MFA a step-up goes straight to review
        enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, decision)
    assert send(client, alice, payee, 10_000).json()["message"] == message


def test_a_second_escalation_after_one_already_won_returns_the_state_and_holds_once(
    app: FastAPI,
    client: TestClient,
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
    settings: Settings,
) -> None:
    """The 'a concurrent request won' branch of _escalate_to_review, driven deterministically: two
    requests that both counted the third wrong code each escalate; the second must report the state
    the first left and must not place a second hold (money reserved twice)."""
    alice, _bob, payee = pair
    enable_mfa(auth_service_factory, migrated_engine, alice, clock)
    force_decision(app, FraudDecision.STEP_UP)
    tid = uuid.UUID(send(client, alice, payee, 70_000).json()["id"])
    meta = RequestMeta(ip="198.51.100.7", user_agent="race-test", request_id="race-1")
    results = []
    for _ in range(2):
        with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
            service = PaymentsService(
                session,
                settings,
                FixedDecisionScorer(FraudDecision.ALLOW),
                auth_service_factory(session),
            )
            results.append(service._escalate_to_review(alice.user_id, tid, meta))
            session.commit()
    assert [r.body["status"] for r in results] == ["PENDING_REVIEW", "PENDING_REVIEW"]
    assert results[1].body["id"] == str(tid)
    assert balance(client, alice)["held"] == 70_000  # one hold, not two
