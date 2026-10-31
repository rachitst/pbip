"""TASK-045: EMI arithmetic is exact in integer paise (Hypothesis properties + known values)."""

from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pbip_api.modules.loans import emi as emi_math

pytestmark = pytest.mark.unit

principals = st.integers(min_value=1_000_00, max_value=10_00_00_000_00)
rates = st.integers(min_value=0, max_value=3_000)  # 0% to 30% p.a.
tenures = st.integers(min_value=1, max_value=480)


@settings(max_examples=300, deadline=None)
@given(principals, rates, tenures)
def test_principal_repaid_always_equals_the_loan(principal: int, rate: int, months: int) -> None:
    plan = emi_math.schedule(principal, rate, months)
    assert sum(i.principal_minor for i in plan.instalments) == principal
    assert plan.instalments[-1].balance_after_minor == 0
    assert len(plan.instalments) == months
    assert plan.total_interest_minor == sum(i.interest_minor for i in plan.instalments)
    assert plan.total_payable_minor == principal + plan.total_interest_minor
    balances = [principal] + [i.balance_after_minor for i in plan.instalments]
    assert all(a >= b >= 0 for a, b in pairwise(balances))
    assert all(i.payment_minor > 0 for i in plan.instalments)  # no empty months at the end
    for i in plan.instalments:
        assert isinstance(i.payment_minor, int) and isinstance(i.interest_minor, int)
        assert i.payment_minor == i.principal_minor + i.interest_minor
    assert all(i.payment_minor == plan.emi_minor for i in plan.instalments[:-1])


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=1_000_00, max_value=5_00_000_00), rates, tenures)
def test_max_principal_is_the_largest_loan_within_the_budget(
    budget: int, rate: int, months: int
) -> None:
    amount = emi_math.max_principal(budget, rate, months)
    assert amount > 0
    assert emi_math.emi(amount, rate, months) <= budget
    assert emi_math.emi(amount + 1, rate, months) > budget


def test_known_emi_values() -> None:
    # INR 1,00,000 at 12% p.a. for 12 months: exact EMI INR 8,884.8789, rounded down to 8,884.87
    assert emi_math.emi(1_00_000_00, 1_200, 12) == 8_884_87
    plan = emi_math.schedule(1_00_000_00, 1_200, 12)
    assert plan.instalments[-1].payment_minor - plan.emi_minor < 12  # under a paisa per month
    assert emi_math.emi(1_20_000_00, 0, 12) == 10_000_00  # interest-free
    with pytest.raises(ValueError, match="positive"):
        emi_math.emi(0, 1_000, 12)


@pytest.mark.parametrize(
    ("principal", "rate", "months"),
    [(187_463, 2_561, 479), (175_775, 2_992, 419), (132_216, 2_718, 452)],
)
def test_no_instalment_exceeds_the_emi_when_interest_rounds_above_it(
    principal: int, rate: int, months: int
) -> None:
    """Regression: the floored EMI fell a paisa below the month's interest; every regular month
    then charged EMI + 1 with no principal and the last instalment repaid the whole loan."""
    plan = emi_math.schedule(principal, rate, months)
    assert all(i.payment_minor == plan.emi_minor for i in plan.instalments[:-1])
    assert sum(i.principal_minor for i in plan.instalments) == principal
    assert plan.instalments[-1].balance_after_minor == 0
    assert plan.instalments[-1].payment_minor < principal + 2 * plan.emi_minor
