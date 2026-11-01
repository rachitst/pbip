"""The transfer flow end to end: validation, idempotency, limits, fraud decisions, step-up, review."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.modules.accounts.models import AccountType
from pbip_api.modules.accounts.service import open_account
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.fraud.dependencies import get_fraud_scorer
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger import service as ledger
from pbip_api.modules.ledger.models import Transaction
from pbip_ml.fraud.contract import FraudDecision
from pbip_ml.fraud.testing import FixedDecisionScorer
from pbip_test_support import Actor, FakeClock, age_payee, totp_code
from sqlalchemy import Engine, func, select, text
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
) -> None:
    alice, _bob, payee = pair
    force_decision(app, FraudDecision.BLOCK)
    response = send(client, alice, payee, 10_000)
    assert response.status_code == 403 and response.json()["status"] == "DECLINED"
    assert "reasons" not in response.text and "risk" not in response.text
    assert balance(client, alice)["ledger"] == 10_000_000
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
