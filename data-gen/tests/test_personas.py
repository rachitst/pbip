import math

import pytest
from pbip_datagen.personas import PERSONAS
from pbip_datagen.rng import make_rng

pytestmark = pytest.mark.unit


def test_persona_shares_sum_to_one() -> None:
    assert math.isclose(sum(p.share for p in PERSONAS), 1.0)


@pytest.mark.parametrize("persona", PERSONAS, ids=lambda p: p.name)
def test_category_weights_sum_to_one(persona: object) -> None:
    weights = persona.category_weights  # type: ignore[attr-defined]
    assert math.isclose(sum(weights.values()), 1.0)


def test_same_seed_gives_identical_data() -> None:
    assert (make_rng(42).random(5) == make_rng(42).random(5)).all()
    assert (make_rng(42).random(5) != make_rng(43).random(5)).any()
