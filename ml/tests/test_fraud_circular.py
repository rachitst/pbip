"""Tests for graph-based circular transfer and mule ring detection (TASK-072, F11)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import networkx as nx
import pytest
from pbip_datagen.fraud import Transfer
from pbip_ml.fraud import circular as c

BASE_TIME = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _make_transfer(
    src: UUID,
    tgt: UUID,
    amount_minor: int,
    occurred_at: datetime = BASE_TIME,
    direction: str = "DEBIT",
    tx_id: UUID | None = None,
) -> Transfer:
    return Transfer(
        id=tx_id or uuid4(),
        customer_id=src if direction == "DEBIT" else tgt,
        occurred_at=occurred_at,
        direction=direction,  # type: ignore[arg-type]
        amount_minor=amount_minor,
        counterparty_id=tgt if direction == "DEBIT" else src,
        payee_added_at=occurred_at - timedelta(days=35),
        device_id=uuid4(),
        new_device=False,
        ip_country="IN",
        ip_city="Mumbai",
        failed_logins_24h=0,
    )


# ---------------------------------------------------------------------------
# 1. Graph Construction Tests
# ---------------------------------------------------------------------------


def test_build_graph_empty_input() -> None:
    graph = c.build_transaction_graph([])
    assert isinstance(graph, nx.DiGraph)
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


def test_build_graph_directed_fund_movement() -> None:
    u1, u2 = uuid4(), uuid4()
    t_debit = _make_transfer(u1, u2, 10_000_00, direction="DEBIT")
    graph = c.build_transaction_graph([t_debit])
    assert graph.has_edge(u1, u2)
    assert not graph.has_edge(u2, u1)
    assert graph[u1][u2]["amount_minor"] == 10_000_00
    assert graph[u1][u2]["transaction_ids"] == [t_debit.id]

    # Test CREDIT direction: counterparty sent to customer
    u3, u4 = uuid4(), uuid4()
    t_credit = _make_transfer(u3, u4, 25_000_00, direction="CREDIT")
    graph2 = c.build_transaction_graph([t_credit])
    assert graph2.has_edge(u3, u4)
    assert graph2[u3][u4]["amount_minor"] == 25_000_00


def test_build_graph_accumulates_multiple_transfers_on_same_edge() -> None:
    u1, u2 = uuid4(), uuid4()
    t1 = _make_transfer(u1, u2, 5_000_00, BASE_TIME - timedelta(hours=2))
    t2 = _make_transfer(u1, u2, 7_000_00, BASE_TIME - timedelta(hours=1))
    graph = c.build_transaction_graph([t1, t2])

    assert graph.number_of_edges() == 1
    edge = graph[u1][u2]
    assert edge["amount_minor"] == 12_000_00
    assert edge["transaction_ids"] == [t1.id, t2.id]
    assert edge["first_occurred_at"] == t1.occurred_at
    assert edge["last_occurred_at"] == t2.occurred_at
    assert len(edge["transfers"]) == 2


def test_build_graph_ignores_duplicate_transaction_replay() -> None:
    u1, u2 = uuid4(), uuid4()
    fixed_id = uuid4()
    t1 = _make_transfer(u1, u2, 5_000_00, BASE_TIME, tx_id=fixed_id)
    t2 = _make_transfer(u1, u2, 5_000_00, BASE_TIME, tx_id=fixed_id)  # Exact duplicate replay

    graph = c.build_transaction_graph([t1, t2])
    edge = graph[u1][u2]
    assert edge["amount_minor"] == 5_000_00  # Not double-counted
    assert len(edge["transaction_ids"]) == 1
    assert len(edge["transfers"]) == 1


def test_build_graph_lookback_window_filtering() -> None:
    u1, u2 = uuid4(), uuid4()
    recent_tx = _make_transfer(u1, u2, 1_000_00, BASE_TIME - timedelta(days=2))
    old_tx = _make_transfer(u1, u2, 9_000_00, BASE_TIME - timedelta(days=20))

    graph = c.build_transaction_graph(
        [recent_tx, old_tx], lookback_window=timedelta(days=7), as_of=BASE_TIME
    )
    assert graph.has_edge(u1, u2)
    assert graph[u1][u2]["amount_minor"] == 1_000_00
    assert graph[u1][u2]["transaction_ids"] == [recent_tx.id]


def test_build_graph_omits_invalid_and_self_transfers() -> None:
    u1, u2 = uuid4(), uuid4()
    zero_tx = _make_transfer(u1, u2, 0)
    negative_tx = _make_transfer(u1, u2, -100)
    self_tx = _make_transfer(u1, u1, 5_000_00)

    graph = c.build_transaction_graph([zero_tx, negative_tx, self_tx])
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


def test_build_graph_supports_dict_records() -> None:
    u1, u2 = uuid4(), uuid4()
    tx_id = uuid4()
    record = {
        "id": tx_id,
        "customer_id": u1,
        "counterparty_id": u2,
        "amount_minor": 15_000_00,
        "occurred_at": BASE_TIME,
        "direction": "DEBIT",
    }
    graph = c.build_transaction_graph([record])
    assert graph.has_edge(u1, u2)
    assert graph[u1][u2]["amount_minor"] == 15_000_00
    assert graph[u1][u2]["transaction_ids"] == [tx_id]


# ---------------------------------------------------------------------------
# 2. Cycle Detection Tests (Lengths 3, 4, 5 and Chronology)
# ---------------------------------------------------------------------------


def test_detect_cycles_lengths_3_4_5() -> None:
    # 3-node cycle with forward chronological progression (+2h per hop)
    nodes3 = [uuid4() for _ in range(3)]
    txs3 = [
        _make_transfer(
            nodes3[i],
            nodes3[(i + 1) % 3],
            10_000_00,
            BASE_TIME + timedelta(hours=2 * i),
        )
        for i in range(3)
    ]
    graph3 = c.build_transaction_graph(txs3)
    loops3 = c.detect_circular_loops(graph3)
    assert len(loops3) == 1
    assert loops3[0].length == 3
    assert loops3[0].node_set == frozenset(nodes3)
    assert loops3[0].retention_ratio == 1.0
    assert loops3[0].chronological_start_node == nodes3[0]

    # 4-node cycle (+2h per hop)
    nodes4 = [uuid4() for _ in range(4)]
    txs4 = [
        _make_transfer(
            nodes4[i],
            nodes4[(i + 1) % 4],
            10_000_00,
            BASE_TIME + timedelta(hours=2 * i),
        )
        for i in range(4)
    ]
    graph4 = c.build_transaction_graph(txs4)
    loops4 = c.detect_circular_loops(graph4)
    assert len(loops4) == 1
    assert loops4[0].length == 4
    assert loops4[0].node_set == frozenset(nodes4)

    # 5-node cycle (+2h per hop)
    nodes5 = [uuid4() for _ in range(5)]
    txs5 = [
        _make_transfer(
            nodes5[i],
            nodes5[(i + 1) % 5],
            10_000_00,
            BASE_TIME + timedelta(hours=2 * i),
        )
        for i in range(5)
    ]
    graph5 = c.build_transaction_graph(txs5)
    loops5 = c.detect_circular_loops(graph5)
    assert len(loops5) == 1
    assert loops5[0].length == 5
    assert loops5[0].node_set == frozenset(nodes5)


def test_detect_cycles_rejects_lengths_1_2_and_6() -> None:
    # Length 2 reciprocal mutual transfer
    u1, u2 = uuid4(), uuid4()
    t1 = _make_transfer(u1, u2, 10_000_00, BASE_TIME)
    t2 = _make_transfer(u2, u1, 10_000_00, BASE_TIME + timedelta(hours=1))
    graph2 = c.build_transaction_graph([t1, t2])
    loops2 = c.detect_circular_loops(graph2)
    assert len(loops2) == 0

    # Length 6 cycle
    nodes6 = [uuid4() for _ in range(6)]
    txs6 = [
        _make_transfer(
            nodes6[i],
            nodes6[(i + 1) % 6],
            10_000_00,
            BASE_TIME + timedelta(hours=i),
        )
        for i in range(6)
    ]
    graph6 = c.build_transaction_graph(txs6)
    loops6 = c.detect_circular_loops(graph6)
    assert len(loops6) == 0


def test_detect_cycles_canonical_deduplication() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_00, BASE_TIME),
        _make_transfer(u2, u3, 10_000_00, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 10_000_00, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 1
    assert loops[0].nodes == c._normalize_cycle([u1, u2, u3])


def test_detect_cycles_handles_overlapping_rings() -> None:
    u1, u2, u3, u4, u5 = [uuid4() for _ in range(5)]
    txs = [
        _make_transfer(u1, u2, 10_000_00, BASE_TIME),
        _make_transfer(u2, u3, 10_000_00, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 10_000_00, BASE_TIME + timedelta(hours=4)),
        _make_transfer(u1, u4, 10_000_00, BASE_TIME + timedelta(hours=1)),
        _make_transfer(u4, u5, 10_000_00, BASE_TIME + timedelta(hours=3)),
        _make_transfer(u5, u1, 10_000_00, BASE_TIME + timedelta(hours=5)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 2
    sets = {loop.node_set for loop in loops}
    assert frozenset([u1, u2, u3]) in sets
    assert frozenset([u1, u4, u5]) in sets


# ---------------------------------------------------------------------------
# 3. Adversarial Chronological Negative Tests
# ---------------------------------------------------------------------------


def test_adversarial_rejected_when_timestamps_are_reversed() -> None:
    """Equal total volume on all edges (100% volume ratio), but timestamps flow backwards in time.

    A -> B on Day 10, B -> C on Day 5, C -> A on Day 1.
    Must NOT be detected as a circular fund loop.
    """
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME - timedelta(days=1)),
        _make_transfer(u2, u3, 10_000_000, BASE_TIME - timedelta(days=5)),
        _make_transfer(u3, u1, 10_000_000, BASE_TIME - timedelta(days=10)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 0  # Broken causality: C paid A before B paid C


def test_adversarial_rejected_when_same_day_hours_are_reversed() -> None:
    """Same day, but reverse hour ordering prevents causal forwarding."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME.replace(hour=16)),
        _make_transfer(u2, u3, 10_000_000, BASE_TIME.replace(hour=10)),  # 6h before receipt
        _make_transfer(u3, u1, 10_000_000, BASE_TIME.replace(hour=20)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 0


def test_adversarial_rejected_hop_delay_exceeds_maximum() -> None:
    """Transfers in forward order but hop delay exceeds configured limit (unrelated payments)."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME - timedelta(days=20)),
        _make_transfer(u2, u3, 10_000_000, BASE_TIME - timedelta(days=10)),  # 10 days delay (> 7d)
        _make_transfer(u3, u1, 10_000_000, BASE_TIME - timedelta(days=2)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph, max_hop_delay=timedelta(days=7))
    assert len(loops) == 0


def test_adversarial_rejected_unrelated_scattered_transfers_with_equal_aggregate_volume() -> None:
    """Edge totals are all ₹1,00,000, but transaction amounts and timestamps are scattered."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    txs = []
    # Leg 1: 10 transactions of ₹10,000
    for i in range(10):
        txs.append(
            _make_transfer(u1, u2, 1_000_000, BASE_TIME - timedelta(days=2) + timedelta(hours=i))
        )
    # Leg 2: 1 transaction of ₹1,00,000
    txs.append(_make_transfer(u2, u3, 10_000_000, BASE_TIME - timedelta(days=2, hours=4)))
    # Leg 3: 2 transactions of ₹50,000
    txs.append(_make_transfer(u3, u1, 5_000_000, BASE_TIME - timedelta(days=3)))
    txs.append(_make_transfer(u3, u1, 5_000_000, BASE_TIME - timedelta(days=3, hours=2)))

    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 0


# ---------------------------------------------------------------------------
# 4. Funds Retention Tests
# ---------------------------------------------------------------------------


def test_retention_above_90_qualifies() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    # 95% retention with chronological progression: ₹1,00,000 -> ₹98,000 -> ₹95,000
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_800_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_500_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 1
    assert loops[0].retention_ratio == pytest.approx(0.95, rel=1e-5)
    assert loops[0].bottleneck_minor == 9_500_000
    assert loops[0].peak_minor == 10_000_000


def test_retention_exactly_90_is_rejected() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    # Exact 90.0%: 9_000_000 / 10_000_000 = 0.9000 -> strictly > 0.90 required
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_000_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph, min_retention=0.90)
    assert len(loops) == 0


def test_retention_below_90_is_rejected() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    # 89.9%: 8_990_000 / 10_000_000 = 0.899
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 8_990_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph, min_retention=0.90)
    assert len(loops) == 0


def test_retention_boundary_slightly_above_90_qualifies() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    # 90.01%: 9_001_000 / 10_000_000 = 0.9001 > 0.90
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_001_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph, min_retention=0.90)
    assert len(loops) == 1
    assert loops[0].retention_ratio > 0.90


# ---------------------------------------------------------------------------
# 5. Risk Scoring Tests
# ---------------------------------------------------------------------------


def test_risk_boost_applied_only_to_participating_accounts() -> None:
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    unrelated = uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_800_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_500_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    boosts = c.calculate_risk_boosts(loops, base_boost=35)

    assert len(boosts) == 3
    for node in (u1, u2, u3):
        assert node in boosts
        assert boosts[node].risk_boost == 35
        assert boosts[node].loop_count == 1
        assert boosts[node].highest_retention == pytest.approx(0.95, rel=1e-5)
        assert len(boosts[node].transaction_ids) == 3

    assert unrelated not in boosts


def test_risk_boost_overlapping_loops_single_boost_prevent_duplicate() -> None:
    u1, u2, u3, u4, u5 = [uuid4() for _ in range(5)]
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_800_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_500_000, BASE_TIME + timedelta(hours=4)),  # 95% retention
        _make_transfer(u1, u4, 5_000_000, BASE_TIME + timedelta(hours=1)),
        _make_transfer(u4, u5, 4_900_000, BASE_TIME + timedelta(hours=3)),
        _make_transfer(u5, u1, 4_800_000, BASE_TIME + timedelta(hours=5)),  # 96% retention
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)
    boosts = c.calculate_risk_boosts(loops, base_boost=35)

    assert boosts[u1].risk_boost == 35
    assert boosts[u1].loop_count == 2
    assert boosts[u1].highest_retention == pytest.approx(0.96, rel=1e-5)
    assert len(boosts[u1].reasons) == 2


def test_circular_mule_ring_rule_adapter() -> None:
    u1 = uuid4()
    boost = c.AccountRiskBoost(
        account_id=u1,
        risk_boost=35,
        loop_count=1,
        highest_retention=0.95,
        reasons=("Participates in a 3-node circular mule ring with 95.0% funds retention",),
        transaction_ids=(uuid4(),),
    )
    rule = c.CircularMuleRingRule(known_boosts={u1: boost})

    triggered, weight, reason = rule.evaluate(u1)
    assert triggered is True
    assert weight == 35
    assert "95.0%" in reason

    triggered_clean, weight_clean, reason_clean = rule.evaluate(uuid4())
    assert triggered_clean is False
    assert weight_clean == 0
    assert reason_clean == ""


# ---------------------------------------------------------------------------
# 6. Ground-Truth Evaluation & 100% Precision Tests
# ---------------------------------------------------------------------------


def test_synthetic_evaluation_achieves_100_percent_precision() -> None:
    transfers, ground_truth_rings = c.generate_circular_ring_evaluation_dataset(seed=42)
    metrics = c.evaluate_circular_detection(
        transfers,
        ground_truth_rings,
        lookback_window=timedelta(days=14),
        min_retention=0.90,
    )

    assert metrics["ground_truth_count"] == 4
    assert metrics["detected_count"] == 4
    assert metrics["true_positives"] == 4
    assert metrics["false_positives"] == 0
    assert metrics["false_negatives"] == 0
    assert metrics["precision"] == 1.0  # Exactly 100% precision
    assert metrics["recall"] == 1.0  # Exactly 100% recall


def test_evaluation_reproducible_across_different_seeds() -> None:
    for seed in (101, 2024, 777):
        transfers, gt_rings = c.generate_circular_ring_evaluation_dataset(seed=seed)
        metrics = c.evaluate_circular_detection(transfers, gt_rings)
        assert metrics["false_positives"] == 0
        assert metrics["precision"] == 1.0


def test_evaluation_with_large_background_transfers() -> None:
    """Inject circular rings into a larger set of random non-circular transfers."""
    base_transfers, gt_rings = c.generate_circular_ring_evaluation_dataset(seed=999)

    # Add 40 random non-circular background transfers between distinct customers
    rng_nodes = [uuid5(UUID(int=12345), f"bg_cust_{i}") for i in range(50)]
    bg_transfers = []
    for i in range(40):
        src = rng_nodes[i % 50]
        tgt = rng_nodes[(i + 13) % 50]
        bg_transfers.append(
            _make_transfer(
                src,
                tgt,
                2_000_00 + i * 100_00,
                BASE_TIME - timedelta(days=i % 10),
            )
        )

    all_transfers = list(base_transfers) + bg_transfers
    metrics = c.evaluate_circular_detection(all_transfers, gt_rings)

    assert metrics["false_positives"] == 0
    assert metrics["precision"] == 1.0
    assert metrics["true_positives"] == len(gt_rings)


# ---------------------------------------------------------------------------
# 7. Targeted Boundary, Timestamp & Precision Tests
# ---------------------------------------------------------------------------


def test_identical_timestamps_different_tx_ids_deterministic() -> None:
    """Simultaneous transfers with identical timestamps across hops are causally accepted
    and tie-broken deterministically by transaction ID."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    simultaneous_time = BASE_TIME
    t1 = _make_transfer(u1, u2, 10_000_000, simultaneous_time, tx_id=uuid4())
    t2 = _make_transfer(u2, u3, 9_500_000, simultaneous_time, tx_id=uuid4())
    t3 = _make_transfer(u3, u1, 9_500_000, simultaneous_time, tx_id=uuid4())

    graph = c.build_transaction_graph([t1, t2, t3])
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 1
    assert loops[0].retention_ratio == 0.95
    assert len(loops[0].transaction_ids) == 3


def test_hop_delay_boundary_exactly_7_days_qualifies() -> None:
    """Hop delay of exactly 7 days (7d 0h 0m 0s) is within max_hop_delay limit."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    t1 = _make_transfer(u1, u2, 10_000_000, BASE_TIME - timedelta(days=14))
    t2 = _make_transfer(u2, u3, 9_500_000, BASE_TIME - timedelta(days=7))
    t3 = _make_transfer(u3, u1, 9_500_000, BASE_TIME)

    graph = c.build_transaction_graph([t1, t2, t3])
    loops = c.detect_circular_loops(graph, max_hop_delay=timedelta(days=7))
    assert len(loops) == 1
    assert loops[0].retention_ratio == 0.95


def test_hop_delay_boundary_beyond_7_days_rejected() -> None:
    """Hop delay of 7 days and 1 second exceeds max_hop_delay limit and must be rejected."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    t1 = _make_transfer(u1, u2, 10_000_000, BASE_TIME - timedelta(days=14, seconds=2))
    t2 = _make_transfer(u2, u3, 9_500_000, BASE_TIME - timedelta(days=7))  # 7d 2s delay (> 7d)
    t3 = _make_transfer(u3, u1, 9_500_000, BASE_TIME)

    graph = c.build_transaction_graph([t1, t2, t3])
    loops = c.detect_circular_loops(graph, max_hop_delay=timedelta(days=7))
    assert len(loops) == 0


def test_timezone_aware_and_naive_timestamp_standardization() -> None:
    """Timezone-naive datetimes and offset-aware datetimes are safely standardized to UTC."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    naive_dt = datetime(2026, 10, 26, 10, 0, tzinfo=None)  # noqa: DTZ001
    rec1 = {
        "id": uuid4(),
        "customer_id": u1,
        "counterparty_id": u2,
        "amount_minor": 10_000_000,
        "occurred_at": naive_dt,
        "direction": "DEBIT",
    }
    rec2 = {
        "id": uuid4(),
        "customer_id": u2,
        "counterparty_id": u3,
        "amount_minor": 9_500_000,
        "occurred_at": naive_dt + timedelta(hours=2),
        "direction": "DEBIT",
    }
    rec3 = {
        "id": uuid4(),
        "customer_id": u3,
        "counterparty_id": u1,
        "amount_minor": 9_500_000,
        "occurred_at": (naive_dt + timedelta(hours=4)).replace(tzinfo=UTC),
        "direction": "DEBIT",
    }

    graph = c.build_transaction_graph([rec1, rec2, rec3])
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 1
    assert loops[0].retention_ratio == 0.95


def test_strict_integer_retention_boundary_paise_precision() -> None:
    """Exact integer check for > 90.0%: 1 paisa above qualifies, 1 paisa below is rejected."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()

    # 1 paisa above 90%: 9_000_001 / 10_000_000 = 0.9000001
    txs_pass = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_000_001, BASE_TIME + timedelta(hours=4)),
    ]
    graph_pass = c.build_transaction_graph(txs_pass)
    loops_pass = c.detect_circular_loops(graph_pass, min_retention=0.90)
    assert len(loops_pass) == 1

    # 1 paisa below 90%: 8_999_999 / 10_000_000 = 0.8999999
    txs_fail = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 8_999_999, BASE_TIME + timedelta(hours=4)),
    ]
    graph_fail = c.build_transaction_graph(txs_fail)
    loops_fail = c.detect_circular_loops(graph_fail, min_retention=0.90)
    assert len(loops_fail) == 0


def test_partial_forwarding_multiple_transfers_no_double_counting() -> None:
    """Multiple transfers on an edge contribute their exact sum and cannot be duplicated."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    fixed_id = uuid4()
    t0a = _make_transfer(u1, u2, 5_000_000, BASE_TIME, tx_id=fixed_id)
    t0a_dup = _make_transfer(u1, u2, 5_000_000, BASE_TIME, tx_id=fixed_id)  # Replay!
    t0b = _make_transfer(u1, u2, 5_000_000, BASE_TIME + timedelta(minutes=30))  # Leg 0 total = 10M
    t1 = _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2))
    t2 = _make_transfer(u3, u1, 9_200_000, BASE_TIME + timedelta(hours=4))

    graph = c.build_transaction_graph([t0a, t0a_dup, t0b, t1, t2])
    # Edge u1 -> u2 must have exactly 10M, not 15M
    assert graph[u1][u2]["amount_minor"] == 10_000_000
    loops = c.detect_circular_loops(graph)
    assert len(loops) == 1
    assert loops[0].retention_ratio == 0.92


def test_risk_boost_capping_and_non_participating_accounts() -> None:
    """Risk boost is capped at MAX_SCORE (100) and non-participating accounts get 0."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    outside_acct = uuid4()
    txs = [
        _make_transfer(u1, u2, 10_000_000, BASE_TIME),
        _make_transfer(u2, u3, 9_500_000, BASE_TIME + timedelta(hours=2)),
        _make_transfer(u3, u1, 9_200_000, BASE_TIME + timedelta(hours=4)),
    ]
    graph = c.build_transaction_graph(txs)
    loops = c.detect_circular_loops(graph)

    # Base boost configured higher than MAX_SCORE (e.g. 150)
    boosts = c.calculate_risk_boosts(loops, base_boost=150)
    assert boosts[u1].risk_boost == 100  # Capped at MAX_SCORE
    assert outside_acct not in boosts

    rule = c.CircularMuleRingRule(known_boosts=boosts)
    trig, weight, reason = rule.evaluate(u1)
    assert trig is True
    assert weight == 100
    assert "92.0%" in reason

    trig_out, weight_out, reason_out = rule.evaluate(outside_acct)
    assert trig_out is False
    assert weight_out == 0
    assert reason_out == ""
