"""TASK-033 (ADR-0019): the hybrid scorer on the transfer and bill-payment paths.

A stub engine pins the model's risk, so every transition of the transfer state machine is driven by
the HYBRID path: ALLOW -> POSTED, STEP_UP -> PENDING_STEP_UP (TOTP), REVIEW -> held + alert,
BLOCK -> declined + alert. Then: the rules are a floor the model cannot lower; every model problem
falls back to the rules (never ALLOW-all); bill payments use the same score; and real trained
artifacts load from disk and score within the latency budget.
"""

import statistics
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pbip_api.core.config import Settings
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.auth.service import AuthService, RequestMeta
from pbip_api.modules.fraud.hybrid import load_engine
from pbip_api.modules.fraud.models import FraudEvent
from pbip_ml.fraud.contract import FraudReason, ReasonSource
from pbip_ml.fraud.hybrid import HybridResult, decide
from pbip_ml.fraud.rules import RuleContext
from pbip_test_support import Actor, FakeClock, age_payee, totp_code
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

STUB_VERSION = "hybrid-stub-1"
MSEB_BILLER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


class StubEngine:
    """Stands in for ``HybridScorer``: a fixed risk with one MODEL reason, or a failure."""

    def __init__(
        self, risk: int, error: Exception | None = None, gate: threading.Event | None = None
    ) -> None:
        self.risk, self.error, self.gate = risk, error, gate
        self.rows: list[dict[str, Any]] = []

    def score(self, row: pd.DataFrame, context: RuleContext) -> HybridResult:
        if self.gate is not None:
            self.gate.wait(10)  # held until the test releases it: a model slower than the budget
        if self.error is not None:
            raise self.error
        self.rows.append(row.iloc[0].to_dict())
        reason = FraudReason(
            code="MODEL_RATIO_TO_AVG_AMOUNT",
            message=f"Model: amount is {context.features.ratio_to_avg_amount:.1f}x the average",
            source=ReasonSource.MODEL,
            contribution=1.25,
        )
        snapshot: dict[str, float | int | str | bool | None] = {  # like the real scorer's
            **{k: float(v) for k, v in row.iloc[0].items()},
            "model_probability": 0.91,
        }
        return HybridResult(self.risk, decide(self.risk), [reason], STUB_VERSION, snapshot)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"fraud_scorer": "hybrid"})


@pytest.fixture
def use_engine(app: FastAPI) -> Iterator[Callable[[object], None]]:
    def install(engine: object) -> None:
        app.state.fraud_engine = engine

    yield install
    app.state.fraud_engine = None


@pytest.fixture
def pair(
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    migrated_engine: Engine,
) -> tuple[Actor, Actor, dict[str, Any]]:
    """Alice (funded) and a 48h-old verified payee for Bob, so the baseline rules stay quiet."""
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)
    age_payee(migrated_engine, payee["id"], 48)
    return alice, bob, payee


def send(client: TestClient, alice: Actor, payee: dict[str, Any], amount: int = 10_000) -> Any:
    return client.post(
        "/api/v1/transfers",
        headers={**alice.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
        json={"from_account_id": str(alice.account_id), "amount_minor": amount,
              "payee_id": payee["id"], "reference": "rent"},
    )  # fmt: skip


def event_for(db: Session, transaction_id: str) -> FraudEvent:
    db.expire_all()
    return db.execute(
        select(FraudEvent).where(FraudEvent.transaction_id == uuid.UUID(transaction_id))
    ).scalar_one()


def held(client: TestClient, actor: Actor) -> int:
    body = client.get(f"/api/v1/accounts/{actor.account_id}", headers=actor.headers).json()
    return int(body["held_minor"])


@pytest.mark.parametrize(
    ("risk", "http", "status"),
    [(10, 201, "POSTED"), (70, 202, "PENDING_REVIEW"), (95, 403, "DECLINED")],
)
def test_hybrid_risk_drives_the_transfer_state_machine(
    client: TestClient,
    use_engine: Callable[[object], None],
    pair: tuple[Actor, Actor, dict[str, Any]],
    make_staff: Callable[..., Actor],
    db: Session,
    risk: int,
    http: int,
    status: str,
) -> None:
    alice, _bob, payee = pair
    use_engine(StubEngine(risk))
    response = send(client, alice, payee)
    assert response.status_code == http and response.json()["status"] == status
    assert "reasons" not in response.text and "risk" not in response.text  # hidden from customers
    event = event_for(db, response.json()["id"])
    assert event.model_version == STUB_VERSION and event.risk_score == risk
    assert event.features["baseline_risk"] == 0 and event.features["hybrid_risk"] == risk
    assert {"ratio_to_avg_amount", "payee_age_minutes", "model_probability"} <= set(event.features)
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    alerts = client.get("/api/v1/fraud/alerts", headers=analyst.headers).json()
    if status == "POSTED":
        assert alerts == [] and held(client, alice) == 0
        return
    assert len(alerts) == 1 and alerts[0]["transaction_status"] == status
    assert alerts[0]["severity"] == ("HIGH" if risk >= 85 else "MEDIUM")
    assert held(client, alice) == (10_000 if status == "PENDING_REVIEW" else 0)
    detail = client.get(f"/api/v1/fraud/alerts/{alerts[0]['id']}", headers=analyst.headers).json()
    model = [r for r in detail["reasons"] if r["source"] == "MODEL"]
    assert model and model[0]["message"].startswith("Model: amount is")  # staff see reasons
    assert detail["model_version"] == STUB_VERSION


def test_hybrid_step_up_requires_the_customers_totp(
    client: TestClient,
    use_engine: Callable[[object], None],
    pair: tuple[Actor, Actor, dict[str, Any]],
    auth_service_factory: Callable[[Session], AuthService],
    migrated_engine: Engine,
    clock: FakeClock,
) -> None:
    alice, _bob, payee = pair
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
        service = auth_service_factory(session)
        secret = service.begin_mfa_enrollment(alice.user_id).secret
        service.confirm_mfa_enrollment(alice.user_id, totp_code(secret, clock), RequestMeta())
    clock.advance(30)
    use_engine(StubEngine(45))
    pending = send(client, alice, payee)
    assert pending.status_code == 202 and pending.json()["status"] == "PENDING_STEP_UP"
    done = client.post(
        f"/api/v1/transfers/{pending.json()['id']}/step-up",
        headers=alice.headers,
        json={"code": totp_code(secret, clock)},
    )
    assert done.status_code == 200 and done.json()["status"] == "POSTED"


def test_the_model_can_raise_but_never_lower_the_rules_decision(
    client: TestClient,
    use_engine: Callable[[object], None],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    db: Session,
) -> None:
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)  # minutes old: NEW_PAYEE_LARGE_AMOUNT (45 points)
    use_engine(StubEngine(0))
    response = send(client, alice, payee, 3_000_000)
    assert response.json()["status"] == "PENDING_REVIEW"  # STEP_UP without MFA -> review
    event = event_for(db, response.json()["id"])
    assert (event.risk_score, event.features["baseline_risk"], event.features["hybrid_risk"]) == (
        45,
        45,
        0,
    )
    assert "NEW_PAYEE_LARGE_AMOUNT" in {r["code"] for r in event.reasons}


def test_model_inputs_come_from_the_platforms_records(
    client: TestClient,
    use_engine: Callable[[object], None],
    pair: tuple[Actor, Actor, dict[str, Any]],
) -> None:
    alice, _bob, payee = pair
    stub = StubEngine(10)
    use_engine(stub)
    for _ in range(3):
        assert send(client, alice, payee, 10_000).status_code == 201
    send(client, alice, payee, 50_000)  # the rules hold this one (burst + spike); inputs matter
    last = stub.rows[-1]
    assert last["velocity_10m"] == 3  # the three earlier debits, all inside 10 minutes
    assert last["ratio_to_avg_amount"] == pytest.approx(5.0)  # 50,000 vs a 10,000 average
    assert last["payee_age_seconds"] >= 48 * 3600 and last["is_debit"] == 1


@pytest.mark.parametrize("problem", ["missing", "raises", "too_slow"])
def test_any_model_problem_falls_back_to_the_rules_never_allow_all(
    client: TestClient,
    app: FastAPI,
    use_engine: Callable[[object], None],
    make_customer: Callable[..., Actor],
    make_verified_payee: Callable[..., dict[str, Any]],
    settings: Settings,
    db: Session,
    problem: str,
) -> None:
    gate = threading.Event()
    engine = {
        "missing": None,
        "raises": StubEngine(0, error=RuntimeError("corrupt model")),
        "too_slow": StubEngine(0, gate=gate),
    }[problem]
    use_engine(engine)
    alice, bob = make_customer(funds=10_000_000), make_customer()
    payee = make_verified_payee(alice, bob)  # risky by the rules alone
    try:
        response = send(client, alice, payee, 3_000_000)
    finally:
        gate.set()  # release the slow call's worker thread
    assert response.json()["status"] == "PENDING_REVIEW"  # still caught, by the rules
    event = event_for(db, response.json()["id"])
    assert event.model_version == "baseline-rules-0.3+rules-only"
    assert "MODEL_UNAVAILABLE" in {r["code"] for r in event.reasons}
    expected = {"missing": "NOT_LOADED", "raises": "RuntimeError", "too_slow": "TIMEOUT"}
    assert event.features["model_error"] == expected[problem]
    if problem == "too_slow":
        assert event.latency_ms < settings.fraud_model_budget_ms + 1_000  # did not wait 10 s


def test_bill_payments_get_the_same_hybrid_score(
    client: TestClient, use_engine: Callable[[object], None], make_customer: Callable[..., Actor],
    db: Session,
) -> None:  # fmt: skip
    alice = make_customer(funds=500_000)
    use_engine(StubEngine(95))
    response = client.post(
        "/api/v1/bills/pay",
        headers={**alice.headers, "Idempotency-Key": f"b-{uuid.uuid4().hex}"},
        json={"from_account_id": str(alice.account_id), "biller_id": str(MSEB_BILLER_ID),
              "consumer_reference": "CA-987654321", "amount_minor": 125_000, "currency": "INR"},
    )  # fmt: skip
    assert response.json()["status"] == "DECLINED"
    event = db.execute(select(FraudEvent).order_by(FraudEvent.created_at.desc())).scalars().first()
    assert event is not None and event.model_version == STUB_VERSION and event.risk_score == 95


# --------------------------------------------------------------------------- real artifacts
@pytest.fixture(scope="session")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Train the real pipeline on a small labelled export (seeded) and write the artifacts."""
    from pbip_datagen.__main__ import _export_fraud
    from pbip_datagen.generator import GeneratorConfig
    from pbip_ml.fraud.train import run

    data, out = tmp_path_factory.mktemp("fraud-data"), tmp_path_factory.mktemp("fraud-model")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=4), data)
    run(data, out)
    return out


def test_real_artifacts_load_once_and_score_within_the_latency_budget(
    client: TestClient,
    use_engine: Callable[[object], None],
    pair: tuple[Actor, Actor, dict[str, Any]],
    settings: Settings,
    model_dir: Path,
    db: Session,
) -> None:
    engine = load_engine(settings.model_copy(update={"fraud_model_dir": str(model_dir)}))
    assert engine is not None and engine.params.model_version == "hybrid-lgbm-iforest-1"
    use_engine(engine)
    alice, _bob, payee = pair
    latencies = []
    first = send(client, alice, payee, 1_000)  # #251: an ordinary transfer to a 2-day-old payee
    first_event = event_for(db, first.json()["id"])
    assert first.json()["status"] == "POSTED", first_event.features
    assert first_event.risk_score < 30 and first_event.model_version == "hybrid-lgbm-iforest-1"
    for _ in range(24):
        response = send(client, alice, payee, 1_000)  # small: the daily limit never bites
        event = event_for(db, response.json()["id"])
        assert event.model_version == "hybrid-lgbm-iforest-1", event.features.get("model_error")
        assert {"model_probability", "anomaly_score", "rule_points"} <= set(event.features)
        latencies.append(event.latency_ms)
    p95 = statistics.quantiles(latencies, n=20)[-1]
    assert p95 < 100, latencies  # contract: p95 < 100 ms, DB look-ups included


def test_load_engine_serves_rules_only_without_valid_artifacts(
    settings: Settings, model_dir: Path, tmp_path: Path
) -> None:
    assert load_engine(settings.model_copy(update={"fraud_scorer": "rules"})) is None
    assert load_engine(settings) is None  # hybrid requested, no directory configured
    assert load_engine(settings.model_copy(update={"fraud_model_dir": str(tmp_path)})) is None
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for f in model_dir.iterdir():
        (tampered / f.name).write_bytes(f.read_bytes())
    (tampered / "hybrid_params.json").write_text('{"model_version": "evil"}', encoding="utf-8")
    assert load_engine(settings.model_copy(update={"fraud_model_dir": str(tampered)})) is None


def test_a_broken_model_stack_never_stops_the_api_from_starting(
    settings: Settings, model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (E2E on #257): the API image lacked libgomp, LightGBM failed to import with an
    OSError, and that crashed start-up instead of falling back to the rules."""
    import builtins

    real_import = builtins.__import__

    def failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pbip_ml.fraud.inference":
            raise OSError("libgomp.so.1: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    configured = settings.model_copy(update={"fraud_model_dir": str(model_dir)})
    assert load_engine(configured) is None
    from pbip_api.main import create_app

    assert create_app(configured).state.fraud_engine is None  # the app still builds
