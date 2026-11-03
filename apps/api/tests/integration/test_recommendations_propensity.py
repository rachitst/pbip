"""ADR-0049: the customer route is ordered by the promoted simulated-uptake model when one is
loaded, says so in the response, still offers only what the rules allow, and falls back to
rules-v1 without one. The RM views stay on rules-v1."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pbip_api.core.db import utcnow
from pbip_api.modules.insights import service as insights_service
from pbip_api.modules.recommendations import service as recommendations_service
from pbip_ml.recommendations import PRODUCT_CATALOGUE
from pbip_ml.recommendations.engine import FAMILY_COVER_ID
from pbip_ml.recommendations.propensity import (
    FEATURE_NAMES,
    MODEL_VERSION,
    ProductModel,
    PropensityModel,
)
from pbip_test_support import Actor
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.security]
N = len(FEATURE_NAMES)


def _model() -> PropensityModel:
    """Every product near 1%, the family cover near 100%: easy to see the re-ordering."""
    return PropensityModel(
        MODEL_VERSION,
        {
            p.product_id: ProductModel(
                8.0 if p.product_id == FAMILY_COVER_ID else -4.6, (0.0,) * N, (0.0,) * N, (1.0,) * N
            )
            for p in PRODUCT_CATALOGUE
        },
    )


@pytest.fixture
def with_model(monkeypatch: pytest.MonkeyPatch) -> PropensityModel:
    model = _model()
    monkeypatch.setattr(recommendations_service, "active_propensity", lambda: model)
    return model


@pytest.fixture
def without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recommendations_service, "active_propensity", lambda: None)


@pytest.mark.usefixtures("without_model")
def test_without_a_model_the_response_says_rules_v1(
    client: TestClient, make_customer: Callable[..., Actor]
) -> None:
    alice = make_customer(salary=100_000_00)

    data = client.get("/api/v1/recommendations", headers=alice.headers).json()

    assert data["model_version"] == "rules-v1"
    assert data["score_from_simulated_data"] is False
    assert "not a trained model" in data["score_version"]
    assert {item["model_version"] for item in data["recommendations"]} == {"rules-v1"}


def test_with_the_model_the_response_says_it_was_trained_on_simulated_uptake(
    client: TestClient,
    make_customer: Callable[..., Actor],
    with_model: PropensityModel,
    db: Session,
) -> None:
    alice = make_customer(salary=100_000_00)
    assert alice.customer_id is not None

    data = client.get("/api/v1/recommendations", headers=alice.headers).json()

    assert data["model_version"] == "sim-propensity-v1"
    assert data["score_from_simulated_data"] is True
    assert "simulated uptake" in data["score_version"]
    assert data["recommendations"][0]["product_id"] == str(FAMILY_COVER_ID)
    assert {item["model_version"] for item in data["recommendations"]} == {"sim-propensity-v1"}
    # Same offers as the rules allow, only re-ordered, with the same reasons.
    everything = recommendations_service.get_customer_recommendations(
        db, alice.customer_id, top_k=len(PRODUCT_CATALOGUE)
    )
    rules = recommendations_service.get_customer_recommendations(
        db, alice.customer_id, top_k=len(PRODUCT_CATALOGUE), propensity=None
    )
    assert {r.product_id: r.reasons for r in everything.recommendations} == {
        r.product_id: r.reasons for r in rules.recommendations
    }


def test_the_rm_views_stay_on_rules_v1_when_the_model_is_loaded(
    make_customer: Callable[..., Actor], with_model: PropensityModel, db: Session
) -> None:
    alice = make_customer(salary=100_000_00)
    assert alice.customer_id is not None

    ranked = insights_service._ranked_products(db, alice.customer_id, utcnow())

    assert ranked
    assert {item.model_version for item in ranked} == {"rules-v1"}
