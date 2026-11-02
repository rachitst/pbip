"""The TASK-086 mutation gate scores mutmut output and refuses incomplete or weak runs."""

from __future__ import annotations

import pytest

from scripts.mutation_gate import parse_results, verdict

pytestmark = pytest.mark.unit

OUTPUT = """
    pbip_api.modules.ledger.service.x_deposit__mutmut_1: killed
    pbip_api.modules.ledger.service.x_deposit__mutmut_2: killed
    pbip_api.modules.payments.service.xǁPaymentsServiceǁcreate_transfer__mutmut_3: killed
    pbip_api.modules.payments.service.xǁPaymentsServiceǁ_lock_user__mutmut_7: survived
some unrelated log line: with a colon
"""


def test_parse_counts_each_mutant_status_and_ignores_other_lines() -> None:
    score = parse_results(OUTPUT)
    assert score.counts == {"killed": 3, "survived": 1}
    assert (score.killed, score.total, score.ratio) == (3, 4, 0.75)


@pytest.mark.parametrize(("threshold", "passes"), [(0.75, True), (0.76, False)])
def test_the_threshold_is_inclusive(threshold: float, passes: bool) -> None:
    ok, message = verdict(parse_results(OUTPUT), threshold)
    assert ok is passes and "3/4 = 75.0%" in message


def test_a_run_with_unchecked_mutants_never_passes() -> None:
    text = OUTPUT + "    pbip_api.modules.ledger.service.x_deposit__mutmut_9: not checked\n"
    ok, message = verdict(parse_results(text), 0.1)
    assert not ok and "1 mutants were not run" in message


def test_no_mutants_is_a_failure_not_a_pass() -> None:
    ok, message = verdict(parse_results(""), 0.0)
    assert not ok and "no mutants found" in message
