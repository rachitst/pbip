"""Integration tests for fraud telemetry and risk analytics endpoints (TASK-035).

Verifies:
- GET /api/v1/fraud/stats returns decision counts (ALLOW, STEP_UP, REVIEW, BLOCK) over time
- Hold rates, block rates, and step-up rates computed accurately
- Breakdown of most frequently triggered rules and high-risk payees
- Responses cached in Redis with 60-second TTL
- Protected by require_roles([FRAUD_ANALYST, ADMIN], mfa=True)
- Input validation on time window parameters
"""

from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.core.redis import get_redis
from pbip_api.modules.auth.models import RoleName
from pbip_api.modules.fraud.models import FraudEvent
from pbip_api.modules.ledger.models import Transaction, TransactionStatus, TransactionType
from pbip_api.modules.payees.models import Payee, PayeeStatus
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_fraud_stats_empty_database(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Empty database returns 200 with zero counts and empty collections."""
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    response = client.get("/api/v1/fraud/stats?days=30", headers=analyst.headers)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["period_days"] == 30
    assert data["total_decisions"] == 0
    assert data["hold_rate"] == 0.0
    assert data["block_rate"] == 0.0
    assert data["decision_counts"] == {"ALLOW": 0, "STEP_UP": 0, "REVIEW": 0, "BLOCK": 0}
    assert data["summary"]["total"] == 0
    assert data["timeline"] == []
    assert data["rules_breakdown"] == []
    assert data["high_risk_payees"] == []


def test_fraud_stats_happy_path_telemetry(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
    db: Session,
) -> None:
    """Computes decision counts, rates, timeline, rule frequencies, and high-risk payees."""
    alice = make_customer(funds=100_000_00)
    analyst = make_staff(RoleName.FRAUD_ANALYST)

    now = utcnow()

    # Create two destination payees
    p1 = Payee(
        customer_id=alice.customer_id,
        name="Target Corp",
        account_number="1111222233",
        bank_code="BARC0001",
        status=PayeeStatus.VERIFIED,
        created_at=now - timedelta(days=20),
    )
    p2 = Payee(
        customer_id=alice.customer_id,
        name="Clean Supplier Ltd",
        account_number="4444555566",
        bank_code="BARC0002",
        status=PayeeStatus.VERIFIED,
        created_at=now - timedelta(days=20),
    )
    db.add_all([p1, p2])
    db.flush()

    # Create transactions with various dates and payees
    txns = [
        # ALLOW 1 (day -5)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.POSTED,
            amount=50_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p2.id,
            created_at=now - timedelta(days=5),
        ),
        # ALLOW 2 (day -5)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.POSTED,
            amount=60_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p2.id,
            created_at=now - timedelta(days=5),
        ),
        # STEP_UP (day -3)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.PENDING_STEP_UP,
            amount=1000_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p1.id,
            created_at=now - timedelta(days=3),
        ),
        # REVIEW (day -2, held)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.PENDING_REVIEW,
            amount=5000_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p1.id,
            created_at=now - timedelta(days=2),
        ),
        # BLOCK (day -1, declined)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.DECLINED,
            amount=20000_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p1.id,
            created_at=now - timedelta(days=1),
        ),
        # BLOCK Old (day -45: outside 30-day window)
        Transaction(
            type=TransactionType.TRANSFER,
            status=TransactionStatus.DECLINED,
            amount=99999_00,
            currency="INR",
            initiated_by_user_id=alice.user_id,
            debit_account_id=alice.account_id,
            payee_id=p1.id,
            created_at=now - timedelta(days=45),
        ),
    ]
    db.add_all(txns)
    db.flush()

    events = [
        # ALLOW 1
        FraudEvent(
            transaction_id=txns[0].id,
            customer_id=alice.customer_id,
            risk_score=15,
            decision="ALLOW",
            reasons=[],
            features={},
            model_version="test-1",
            latency_ms=1.2,
            created_at=now - timedelta(days=5),
        ),
        # ALLOW 2
        FraudEvent(
            transaction_id=txns[1].id,
            customer_id=alice.customer_id,
            risk_score=25,
            decision="ALLOW",
            reasons=[],
            features={},
            model_version="test-1",
            latency_ms=1.4,
            created_at=now - timedelta(days=5),
        ),
        # STEP_UP
        FraudEvent(
            transaction_id=txns[2].id,
            customer_id=alice.customer_id,
            risk_score=60,
            decision="STEP_UP",
            reasons=[{"code": "HIGH_AMOUNT", "source": "RULE", "message": "High amount"}],
            features={},
            model_version="test-1",
            latency_ms=2.1,
            created_at=now - timedelta(days=3),
        ),
        # REVIEW
        FraudEvent(
            transaction_id=txns[3].id,
            customer_id=alice.customer_id,
            risk_score=85,
            decision="REVIEW",
            reasons=[
                {"code": "HIGH_AMOUNT", "source": "RULE", "message": "High amount"},
                {"code": "NEW_PAYEE", "source": "RULE", "message": "New payee detected"},
            ],
            features={},
            model_version="test-1",
            latency_ms=2.5,
            created_at=now - timedelta(days=2),
        ),
        # BLOCK
        FraudEvent(
            transaction_id=txns[4].id,
            customer_id=alice.customer_id,
            risk_score=95,
            decision="BLOCK",
            reasons=[
                {"code": "VELOCITY_EXCEEDED", "source": "RULE", "message": "Velocity exceeded"}
            ],
            features={},
            model_version="test-1",
            latency_ms=3.0,
            created_at=now - timedelta(days=1),
        ),
        # Outside window event
        FraudEvent(
            transaction_id=txns[5].id,
            customer_id=alice.customer_id,
            risk_score=99,
            decision="BLOCK",
            reasons=[{"code": "VELOCITY_EXCEEDED", "source": "RULE", "message": "Old velocity"}],
            features={},
            model_version="test-1",
            latency_ms=2.0,
            created_at=now - timedelta(days=45),
        ),
    ]
    db.add_all(events)
    db.commit()

    response = client.get("/api/v1/fraud/stats?days=30", headers=analyst.headers)
    assert response.status_code == 200, response.text
    data = response.json()

    # Total and decision counts
    assert data["total_decisions"] == 5
    assert data["decision_counts"]["ALLOW"] == 2
    assert data["decision_counts"]["STEP_UP"] == 1
    assert data["decision_counts"]["REVIEW"] == 1
    assert data["decision_counts"]["BLOCK"] == 1

    # Rates
    assert data["hold_rate"] == 0.2  # 1 REVIEW / 5
    assert data["block_rate"] == 0.2  # 1 BLOCK / 5
    assert data["summary"]["step_up_rate"] == 0.2  # 1 STEP_UP / 5

    # Timeline points
    assert len(data["timeline"]) >= 3
    total_timeline_sum = sum(p["total"] for p in data["timeline"])
    assert total_timeline_sum == 5

    # Rules breakdown
    rules = {r["rule_code"]: r for r in data["rules_breakdown"]}
    assert "HIGH_AMOUNT" in rules
    assert rules["HIGH_AMOUNT"]["count"] == 2
    assert rules["HIGH_AMOUNT"]["percentage"] == 40.0
    assert "NEW_PAYEE" in rules
    assert rules["NEW_PAYEE"]["count"] == 1
    assert "VELOCITY_EXCEEDED" in rules
    assert rules["VELOCITY_EXCEEDED"]["count"] == 1

    # High-risk payees
    payees = data["high_risk_payees"]
    assert len(payees) >= 1
    assert payees[0]["payee_id"] == str(p1.id)
    assert payees[0]["payee_name"] == "Target Corp"
    assert payees[0]["flagged_count"] == 3  # STEP_UP, REVIEW, BLOCK
    assert payees[0]["avg_risk_score"] == 80.0  # (60 + 85 + 95) / 3 = 80.0


def test_fraud_stats_redis_caching(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Responses are cached in Redis with a 60-second TTL to avoid database strain."""
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    redis_client = client.app.dependency_overrides.get(get_redis, get_redis)()

    cache_key = "fraud:stats:days:30"
    redis_client.delete(cache_key)

    resp1 = client.get("/api/v1/fraud/stats?days=30", headers=analyst.headers)
    assert resp1.status_code == 200

    # Key exists in Redis with TTL <= 60
    assert redis_client.exists(cache_key)
    ttl = redis_client.ttl(cache_key)
    assert 0 < ttl <= 60

    # Second response returns exact cached data
    resp2 = client.get("/api/v1/fraud/stats?days=30", headers=analyst.headers)
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()


def test_fraud_stats_rbac_and_mfa(
    client: TestClient,
    make_customer: Callable[..., Actor],
    make_staff: Callable[..., Actor],
) -> None:
    """Enforces authentication, FRAUD_ANALYST/ADMIN role, and MFA claim."""
    # 401 unauthenticated
    assert client.get("/api/v1/fraud/stats").status_code == 401

    # 403 customer
    alice = make_customer()
    assert client.get("/api/v1/fraud/stats", headers=alice.headers).status_code == 403

    # 403 staff without MFA
    no_mfa = make_staff(RoleName.FRAUD_ANALYST, mfa=False)
    denied = client.get("/api/v1/fraud/stats", headers=no_mfa.headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == "mfa_required"

    # 403 compliance officer (role separation)
    compliance = make_staff(RoleName.COMPLIANCE_OFFICER)
    assert client.get("/api/v1/fraud/stats", headers=compliance.headers).status_code == 403

    # 200 fraud analyst with MFA
    analyst = make_staff(RoleName.FRAUD_ANALYST)
    assert client.get("/api/v1/fraud/stats", headers=analyst.headers).status_code == 200

    # 200 admin with MFA
    admin = make_staff(RoleName.ADMIN)
    assert client.get("/api/v1/fraud/stats", headers=admin.headers).status_code == 200


def test_fraud_stats_parameter_validation(
    client: TestClient,
    make_staff: Callable[..., Actor],
) -> None:
    """Rejects out-of-range days parameters with 422."""
    analyst = make_staff(RoleName.FRAUD_ANALYST)

    # days < 1
    assert client.get("/api/v1/fraud/stats?days=0", headers=analyst.headers).status_code == 422

    # days > 365
    assert client.get("/api/v1/fraud/stats?days=366", headers=analyst.headers).status_code == 422
