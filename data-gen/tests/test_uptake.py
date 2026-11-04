"""The simulated uptake panel (uptake-sim-2, TASK-054 #77): deterministic, prefix-stable, driven
by the documented behaviour, and independent of the recommendation rules it is compared with."""

import ast
import csv
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pbip_datagen import uptake
from pbip_datagen.__main__ import UPTAKE_PANEL_FILE, UPTAKE_SUMMARY_FILE, main
from pbip_datagen.uptake import (
    PRODUCTS,
    Behaviour,
    UptakeAssumptions,
    simulate_panel,
    summary,
    uptake_logit,
)

pytestmark = pytest.mark.unit

SOURCE = Path(uptake.__file__)
A = UptakeAssumptions()


def _behaviour(**overrides: object) -> Behaviour:
    base = Behaviour(
        income_stability=0.5,
        savings_rate=0.1,
        discretionary_share=0.3,
        discretionary_inr=10_000.0,
        buffer_months=2.0,
        age_band="36-50",
        held=frozenset({"SAVINGS_ACCOUNT"}),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]  # keyword overrides of the fields


# --- Independence from rules-v1 (owner's instruction) ---------------------------------------------


def test_the_label_generator_imports_nothing_from_the_recommendation_engine() -> None:
    """No rules-v1 score, eligibility rule or other engine output can feed the labels: the module
    imports only the standard library, NumPy and data-gen's own personas."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported == {
        "math",
        "collections.abc",
        "dataclasses",
        "typing",
        "numpy",
        "pbip_datagen.personas",
    }
    text = SOURCE.read_text(encoding="utf-8")
    for name in ("relevance_score", "evaluate_eligibility", "held_product_rule", "rules_v1"):
        assert name not in text


def test_generating_and_exporting_the_panel_loads_no_pbip_ml_module(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from pbip_datagen.__main__ import export_uptake\n"
        f"export_uptake(1, 3, 4, Path({str(tmp_path)!r}))\n"
        "print(sorted(m for m in sys.modules if m.startswith('pbip_ml')))\n"
    )
    result = subprocess.run(  # noqa: S603 - the interpreter on a fixed probe
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == "[]"


# --- Determinism ------------------------------------------------------------------------------


def test_the_panel_is_deterministic_for_a_seed_and_changes_with_it() -> None:
    assert simulate_panel(3, 20, 8) == simulate_panel(3, 20, 8)
    assert simulate_panel(3, 20, 8) != simulate_panel(4, 20, 8)


def test_a_customers_history_depends_neither_on_the_population_nor_on_later_months() -> None:
    small = [r for r in simulate_panel(3, 5, 8) if r.customer == 4]
    large = [r for r in simulate_panel(3, 40, 12) if r.customer == 4 and r.month < 8]
    assert small == large


def test_rows_hold_integer_paise_binary_uptakes_and_none_in_the_first_month() -> None:
    rows = simulate_panel(5, 60, 10)
    for row in rows:
        assert all(
            isinstance(v, int) for v in (row.income_minor, row.spend_minor, row.balance_minor)
        )
        assert row.balance_minor >= 0
        assert set(row.took) <= {0, 1}
        assert len(row.clicks) == len(row.took) == len(PRODUCTS)
    assert not any(any(r.took) for r in rows if r.month == 0)


def test_one_off_products_are_taken_at_most_once() -> None:
    rows = simulate_panel(11, 300, 18)
    for i, product in enumerate(PRODUCTS):
        if product in {"rewards_card", "index_sip", "family_cover"}:
            per_customer: dict[int, int] = {}
            for row in rows:
                per_customer[row.customer] = per_customer.get(row.customer, 0) + row.took[i]
            assert max(per_customer.values()) <= 1


def test_monthly_uptake_rates_stay_in_the_documented_band() -> None:
    """The base logits were calibrated to 1-4% a month (seed 42, 3,000 x 24); a smaller panel
    with another seed lands inside a wider band."""
    rates = summary(simulate_panel(8, 600, 18))["products"]
    assert isinstance(rates, dict)
    for product in PRODUCTS:
        assert 0.004 <= rates[product]["monthly_rate"] <= 0.07, product


# --- The documented drivers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "low", "high"),
    [
        ("index_sip", {"savings_rate": 0.0}, {"savings_rate": 0.3}),
        ("fixed_deposit", {"income_stability": 0.1}, {"income_stability": 0.9}),
        ("fixed_deposit", {"age_band": "26-35"}, {"age_band": "60+"}),
        ("credit_line", {"savings_rate": 0.1}, {"savings_rate": -0.2}),
        ("rewards_card", {"discretionary_inr": 5_000.0}, {"discretionary_inr": 40_000.0}),
        ("rewards_card", {"discretionary_share": 0.2}, {"discretionary_share": 0.6}),
        ("family_cover", {"held": frozenset()}, {"held": frozenset({"HOME_LOAN"})}),
        ("savings_account", {"held": frozenset({"SAVINGS_ACCOUNT"})}, {"held": frozenset()}),
    ],
)
def test_each_driver_moves_uptake_in_the_documented_direction(
    product: str, low: dict[str, object], high: dict[str, object]
) -> None:
    assert uptake_logit(product, _behaviour(**high), 0.0, 0, A) > uptake_logit(
        product, _behaviour(**low), 0.0, 0, A
    )


def test_clickstream_interest_raises_uptake() -> None:
    b = _behaviour()
    assert uptake_logit("index_sip", b, 1.0, 0, A) > uptake_logit("index_sip", b, 0.0, 0, A)
    assert uptake_logit("index_sip", b, 0.0, 3, A) > uptake_logit("index_sip", b, 0.0, 0, A)


def test_the_held_out_variant_scales_effects_and_shifts_base_rates() -> None:
    variant = uptake.perturbed(A, 0.7, 0.3)
    assert variant.effect_scale == pytest.approx(0.7)
    assert variant.base_logit["index_sip"] == pytest.approx(A.base_logit["index_sip"] + 0.3)


# --- Export -------------------------------------------------------------------------------------


def test_the_cli_writes_the_panel_and_a_summary_labelled_simulated(tmp_path: Path) -> None:
    status = main(
        ["uptake", "--seed", "2", "--customers", "4", "--months", "5", "--out", str(tmp_path)]
    )
    assert status == 0
    with (tmp_path / UPTAKE_PANEL_FILE).open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 20
    assert {f"took_{p}" for p in PRODUCTS} | {f"clicks_{p}" for p in PRODUCTS} <= set(rows[0])
    assert "SIMULATED" in (tmp_path / UPTAKE_SUMMARY_FILE).read_text(encoding="utf-8")


def test_the_cli_refuses_a_panel_too_short_to_label(tmp_path: Path) -> None:
    assert main(["uptake", "--months", "1", "--out", str(tmp_path)]) == 2
