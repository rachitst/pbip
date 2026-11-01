"""Graph-based circular transfer and mule ring detection spike (TASK-072, F11).

Detects circular fund loops between mule accounts using directed transaction graphs
and NetworkX. Evaluates simple directed cycles of length 3-5 with high funds retention
(strictly > 90%), and produces risk score boosts for participating accounts.

Architecture:
- Pure graph analysis operating on directed fund movements.
- Bounded to `pbip_ml.fraud` without depending on the API monolith (import-linter contract).
- Deterministic, side-effect free, and auditable.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import networkx as nx
from pbip_datagen.fraud import Transfer

DEFAULT_MIN_CYCLE_LENGTH = 3
DEFAULT_MAX_CYCLE_LENGTH = 5
DEFAULT_MIN_RETENTION = 0.90
DEFAULT_RISK_BOOST = 35
MAX_SCORE = 100


@dataclass(frozen=True, slots=True)
class CycleEdge:
    """Directed leg of a detected circular fund loop."""

    source: UUID
    target: UUID
    amount_minor: int
    transaction_ids: tuple[UUID, ...]
    first_occurred_at: datetime
    last_occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CircularLoop:
    """A detected simple cycle in the transaction graph satisfying length and retention criteria."""

    nodes: tuple[UUID, ...]
    length: int
    retention_ratio: float
    bottleneck_minor: int
    peak_minor: int
    transaction_ids: tuple[UUID, ...]
    edges: tuple[CycleEdge, ...]

    @property
    def node_set(self) -> frozenset[UUID]:
        """Unordered set of participating accounts for cycle identity matching."""
        return frozenset(self.nodes)


@dataclass(frozen=True, slots=True)
class AccountRiskBoost:
    """Risk score adjustment and evidence for an account participating in circular loops."""

    account_id: UUID
    risk_boost: int
    loop_count: int
    highest_retention: float
    reasons: tuple[str, ...]
    transaction_ids: tuple[UUID, ...]


def _extract_transfer_tuple(
    record: Any,
) -> tuple[UUID, UUID, int, datetime, UUID] | None:
    """Extract (source, target, amount_minor, occurred_at, tx_id) from a transfer record."""
    if isinstance(record, Transfer):
        tx_id = record.id
        occurred_at = record.occurred_at
        amt = record.amount_minor
        if record.direction == "DEBIT":
            src, tgt = record.customer_id, record.counterparty_id
        else:
            src, tgt = record.counterparty_id, record.customer_id
        return (src, tgt, amt, occurred_at, tx_id)

    if isinstance(record, dict):
        tx_id = record["id"]
        occurred_at = record["occurred_at"]
        amt = int(record["amount_minor"])
        direction = str(record.get("direction", "DEBIT"))
        if direction == "DEBIT":
            src, tgt = record["customer_id"], record["counterparty_id"]
        else:
            src, tgt = record["counterparty_id"], record["customer_id"]
        return (src, tgt, amt, occurred_at, tx_id)

    if hasattr(record, "amount_minor") and hasattr(record, "occurred_at"):
        tx_id = record.id
        occurred_at = record.occurred_at
        amt = int(record.amount_minor)
        direction = str(getattr(record, "direction", "DEBIT"))
        cust = record.customer_id
        party = record.counterparty_id
        src, tgt = (cust, party) if direction == "DEBIT" else (party, cust)
        return (src, tgt, amt, occurred_at, tx_id)

    return None


def _is_within_window(
    occurred_at: datetime,
    cutoff: datetime | None,
    as_of: datetime | None,
) -> bool:
    """Check if transaction occurred within the requested lookback window."""
    if cutoff is not None and occurred_at < cutoff:
        return False
    return not (as_of is not None and occurred_at > as_of)


def _add_edge_record(
    graph: nx.DiGraph,
    src: UUID,
    tgt: UUID,
    amt: int,
    occurred_at: datetime,
    tx_id: UUID,
) -> None:
    """Add or accumulate a directed transaction along edge src -> tgt."""
    if graph.has_edge(src, tgt):
        edge_data = graph[src][tgt]
        edge_data["amount_minor"] += amt
        edge_data["transaction_ids"].append(tx_id)
        if occurred_at < edge_data["first_occurred_at"]:
            edge_data["first_occurred_at"] = occurred_at
        if occurred_at > edge_data["last_occurred_at"]:
            edge_data["last_occurred_at"] = occurred_at
    else:
        graph.add_edge(
            src,
            tgt,
            amount_minor=amt,
            transaction_ids=[tx_id],
            first_occurred_at=occurred_at,
            last_occurred_at=occurred_at,
        )


def build_transaction_graph(
    transfers: Iterable[Any],
    *,
    lookback_window: timedelta | None = None,
    as_of: datetime | None = None,
) -> nx.DiGraph:
    """Build a directed graph representing fund movement between accounts.

    Nodes represent account or customer UUIDs.
    Edges (u -> v) represent directed transfers where u sent funds to v.
    """
    graph: nx.DiGraph = nx.DiGraph()
    items = list(transfers)
    if not items:
        return graph

    extracted_records: list[tuple[UUID, UUID, int, datetime, UUID]] = []
    for item in items:
        extracted = _extract_transfer_tuple(item)
        if extracted is not None and extracted[2] > 0 and extracted[0] != extracted[1]:
            extracted_records.append(extracted)

    if not extracted_records:
        return graph

    effective_as_of = as_of
    if lookback_window is not None and effective_as_of is None:
        effective_as_of = max(rec[3] for rec in extracted_records)

    cutoff = (
        (effective_as_of - lookback_window)
        if (lookback_window is not None and effective_as_of is not None)
        else None
    )

    for src, tgt, amt, occurred_at, tx_id in extracted_records:
        if _is_within_window(occurred_at, cutoff, effective_as_of):
            _add_edge_record(graph, src, tgt, amt, occurred_at, tx_id)

    return graph


def _normalize_cycle(cycle: Sequence[UUID]) -> tuple[UUID, ...]:
    """Rotate cycle sequence so the minimum element is first for canonical deduplication."""
    if not cycle:
        return ()
    tup = tuple(cycle)
    min_idx = min(range(len(tup)), key=lambda i: tup[i])
    return tup[min_idx:] + tup[:min_idx]


def calculate_cycle_retention(
    cycle: Sequence[UUID],
    graph: nx.DiGraph,
) -> tuple[float, int, int]:
    """Calculate funds retention ratio for a directed cycle.

    Formula:
        retention = min_leg_volume / max_leg_volume
    Returns (retention_ratio, bottleneck_minor, peak_minor).
    """
    if len(cycle) < 2:
        return (0.0, 0, 0)

    leg_amounts: list[int] = []
    for i in range(len(cycle)):
        u = cycle[i]
        v = cycle[(i + 1) % len(cycle)]
        if not graph.has_edge(u, v):
            return (0.0, 0, 0)
        leg_amounts.append(int(graph[u][v]["amount_minor"]))

    if not leg_amounts:
        return (0.0, 0, 0)

    bottleneck = min(leg_amounts)
    peak = max(leg_amounts)
    if peak <= 0:
        return (0.0, 0, 0)

    ratio = float(bottleneck) / float(peak)
    return (ratio, bottleneck, peak)


def _build_cycle_edges(
    cycle: Sequence[UUID],
    graph: nx.DiGraph,
) -> tuple[CycleEdge, ...]:
    """Construct immutable leg details for a detected cycle."""
    edges: list[CycleEdge] = []
    for i in range(len(cycle)):
        u = cycle[i]
        v = cycle[(i + 1) % len(cycle)]
        data = graph[u][v]
        edges.append(
            CycleEdge(
                source=u,
                target=v,
                amount_minor=int(data["amount_minor"]),
                transaction_ids=tuple(data["transaction_ids"]),
                first_occurred_at=data["first_occurred_at"],
                last_occurred_at=data["last_occurred_at"],
            )
        )
    return tuple(edges)


def detect_circular_loops(
    graph: nx.DiGraph,
    *,
    min_length: int = DEFAULT_MIN_CYCLE_LENGTH,
    max_length: int = DEFAULT_MAX_CYCLE_LENGTH,
    min_retention: float = DEFAULT_MIN_RETENTION,
) -> list[CircularLoop]:
    """Detect simple directed cycles of length 3-5 with retention strictly > min_retention."""
    if graph.number_of_nodes() < min_length or graph.number_of_edges() < min_length:
        return []

    seen_cycles: set[tuple[UUID, ...]] = set()
    qualifying_loops: list[CircularLoop] = []

    # NetworkX simple_cycles enumerates elementary circuits
    for raw_cycle in nx.simple_cycles(graph):
        cycle_len = len(raw_cycle)
        if cycle_len < min_length or cycle_len > max_length:
            continue

        normalized = _normalize_cycle(raw_cycle)
        if normalized in seen_cycles:
            continue
        seen_cycles.add(normalized)

        ratio, bottleneck, peak = calculate_cycle_retention(normalized, graph)
        # Strict inequality: retention must be GREATER than threshold (not >=)
        if ratio > min_retention:
            edge_details = _build_cycle_edges(normalized, graph)
            all_tx_ids: list[UUID] = []
            for edge in edge_details:
                all_tx_ids.extend(edge.transaction_ids)

            qualifying_loops.append(
                CircularLoop(
                    nodes=normalized,
                    length=cycle_len,
                    retention_ratio=ratio,
                    bottleneck_minor=bottleneck,
                    peak_minor=peak,
                    transaction_ids=tuple(all_tx_ids),
                    edges=edge_details,
                )
            )

    return qualifying_loops


def calculate_risk_boosts(
    loops: Sequence[CircularLoop],
    *,
    base_boost: int = DEFAULT_RISK_BOOST,
) -> dict[UUID, AccountRiskBoost]:
    """Calculate risk score boosts for accounts participating in detected circular loops.

    Participating accounts receive a single boost (not multiplied for overlapping cycles).
    Non-participating accounts receive no boost.
    """
    account_loops: dict[UUID, list[CircularLoop]] = defaultdict(list)
    for loop in loops:
        for node in loop.nodes:
            account_loops[node].append(loop)

    boosts: dict[UUID, AccountRiskBoost] = {}
    for acct_id, acct_loop_list in account_loops.items():
        highest_ret = max(item.retention_ratio for item in acct_loop_list)
        all_tx: list[UUID] = []
        for item in acct_loop_list:
            all_tx.extend(item.transaction_ids)
        distinct_tx = tuple(dict.fromkeys(all_tx))

        reasons = tuple(
            f"Participates in a {item.length}-node circular mule ring with "
            f"{item.retention_ratio * 100:.1f}% funds retention"
            for item in acct_loop_list
        )
        boosts[acct_id] = AccountRiskBoost(
            account_id=acct_id,
            risk_boost=min(base_boost, MAX_SCORE),
            loop_count=len(acct_loop_list),
            highest_retention=highest_ret,
            reasons=reasons,
            transaction_ids=distinct_tx,
        )

    return boosts


@dataclass
class CircularMuleRingRule:
    """Explainable rule adapter evaluating participation in detected circular loops."""

    code: str = "CIRCULAR_MULE_RING"
    boost_points: int = DEFAULT_RISK_BOOST
    known_boosts: dict[UUID, AccountRiskBoost] = field(default_factory=dict)

    def evaluate(self, customer_id: UUID) -> tuple[bool, int, str]:
        """Return (triggered, weight, reason)."""
        boost = self.known_boosts.get(customer_id)
        if boost is None:
            return (False, 0, "")
        reason = "; ".join(boost.reasons)
        return (True, boost.risk_boost, reason)


def _make_synth_transfer(
    tx_id: UUID,
    src: UUID,
    tgt: UUID,
    amount_minor: int,
    occurred_at: datetime,
) -> Transfer:
    """Helper creating a minimal Transfer instance for synthetic graph testing."""
    return Transfer(
        id=tx_id,
        customer_id=src,
        occurred_at=occurred_at,
        direction="DEBIT",
        amount_minor=amount_minor,
        counterparty_id=tgt,
        payee_added_at=occurred_at - timedelta(days=40),
        device_id=uuid5(src, "dev-1"),
        new_device=False,
        ip_country="IN",
        ip_city="Mumbai",
        failed_logins_24h=0,
    )


def _generate_ground_truth_rings(
    now: datetime,
    node_id_fn: Any,
    tx_id_fn: Any,
) -> tuple[list[Transfer], set[frozenset[UUID]]]:
    """Generate ground truth circular rings of lengths 3, 4, and 5 with > 90% retention."""
    transfers: list[Transfer] = []
    ground_truth_rings: set[frozenset[UUID]] = set()

    # Ring 1: 3-Node Ring (96% retention)
    r1_nodes = [node_id_fn(f"r1_{i}") for i in range(3)]
    ground_truth_rings.add(frozenset(r1_nodes))
    r1_amounts = [10_000_000, 9_800_000, 9_600_000]
    for i in range(3):
        t_time = now - timedelta(days=2, hours=3 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r1_leg_{i}"),
                r1_nodes[i],
                r1_nodes[(i + 1) % 3],
                r1_amounts[i],
                t_time,
            )
        )

    # Ring 2: 4-Node Ring (94% retention)
    r2_nodes = [node_id_fn(f"r2_{i}") for i in range(4)]
    ground_truth_rings.add(frozenset(r2_nodes))
    r2_amounts = [5_000_000, 4_900_000, 4_800_000, 4_700_000]
    for i in range(4):
        t_time = now - timedelta(days=3, hours=2 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r2_leg_{i}"),
                r2_nodes[i],
                r2_nodes[(i + 1) % 4],
                r2_amounts[i],
                t_time,
            )
        )

    # Ring 3: 5-Node Ring (92.5% retention)
    r3_nodes = [node_id_fn(f"r3_{i}") for i in range(5)]
    ground_truth_rings.add(frozenset(r3_nodes))
    r3_amounts = [20_000_000, 19_500_000, 19_000_000, 18_800_000, 18_500_000]
    for i in range(5):
        t_time = now - timedelta(days=4, hours=4 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r3_leg_{i}"),
                r3_nodes[i],
                r3_nodes[(i + 1) % 5],
                r3_amounts[i],
                t_time,
            )
        )

    # Ring 4: 3-Node Ring with multiple transfers on leg 0 (95% retention)
    r4_nodes = [node_id_fn(f"r4_{i}") for i in range(3)]
    ground_truth_rings.add(frozenset(r4_nodes))
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_0a"),
            r4_nodes[0],
            r4_nodes[1],
            6_000_000,
            now - timedelta(days=1, hours=6),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_0b"),
            r4_nodes[0],
            r4_nodes[1],
            4_000_000,
            now - timedelta(days=1, hours=5),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_1"),
            r4_nodes[1],
            r4_nodes[2],
            9_700_000,
            now - timedelta(days=1, hours=4),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_2"),
            r4_nodes[2],
            r4_nodes[0],
            9_500_000,
            now - timedelta(days=1, hours=3),
        )
    )

    return transfers, ground_truth_rings


def _generate_distractor_transfers(
    now: datetime,
    node_id_fn: Any,
    tx_id_fn: Any,
) -> list[Transfer]:
    """Generate distractor transaction structures that must not be flagged as circular loops."""
    transfers: list[Transfer] = []

    # Distractor 1: Low-retention 3-node cycle (50% retention)
    d1_nodes = [node_id_fn(f"d1_{i}") for i in range(3)]
    d1_amounts = [10_000_000, 5_000_000, 5_000_000]
    for i in range(3):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d1_leg_{i}"),
                d1_nodes[i],
                d1_nodes[(i + 1) % 3],
                d1_amounts[i],
                now - timedelta(days=2),
            )
        )

    # Distractor 2: Exact boundary 90.0% retention cycle (not strictly > 90%)
    d2_nodes = [node_id_fn(f"d2_{i}") for i in range(3)]
    d2_amounts = [10_000_000, 9_500_000, 9_000_000]
    for i in range(3):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d2_leg_{i}"),
                d2_nodes[i],
                d2_nodes[(i + 1) % 3],
                d2_amounts[i],
                now - timedelta(days=2),
            )
        )

    # Distractor 3: Reciprocal 2-node cycle (length 2 mutual transfers)
    d3_a, d3_b = node_id_fn("d3_a"), node_id_fn("d3_b")
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d3_leg_0"), d3_a, d3_b, 5_000_000, now - timedelta(days=1, hours=2)
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d3_leg_1"), d3_b, d3_a, 5_000_000, now - timedelta(days=1, hours=1)
        )
    )

    # Distractor 4: Length 6 cycle (exceeds max length 5)
    d4_nodes = [node_id_fn(f"d4_{i}") for i in range(6)]
    for i in range(6):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d4_leg_{i}"),
                d4_nodes[i],
                d4_nodes[(i + 1) % 6],
                10_000_000,
                now - timedelta(days=2),
            )
        )

    # Distractor 5: Expired 3-node ring (outside 14-day lookback window)
    d5_nodes = [node_id_fn(f"d5_{i}") for i in range(3)]
    for i in range(3):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d5_leg_{i}"),
                d5_nodes[i],
                d5_nodes[(i + 1) % 3],
                10_000_000,
                now - timedelta(days=45),
            )
        )

    # Distractor 6: Linear chain (no loop)
    d6_nodes = [node_id_fn(f"d6_{i}") for i in range(4)]
    for i in range(3):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d6_leg_{i}"),
                d6_nodes[i],
                d6_nodes[i + 1],
                8_000_000,
                now - timedelta(days=1),
            )
        )

    # Distractor 7: Star / Hub-and-Spoke (salary disbursement: 1 sender to 6 receivers)
    hub = node_id_fn("hub_employer")
    for i in range(6):
        worker = node_id_fn(f"worker_{i}")
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"salary_{i}"),
                hub,
                worker,
                5_000_000,
                now - timedelta(days=3),
            )
        )

    return transfers


def generate_circular_ring_evaluation_dataset(
    seed: int = 42,
    base_time: datetime | None = None,
) -> tuple[list[Transfer], set[frozenset[UUID]]]:
    """Generate a deterministic synthetic dataset with ground-truth circular rings
    and distractors.
    """
    now = base_time or datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def node_id(name: str) -> UUID:
        return uuid5(UUID(int=seed), name)

    def tx_id(name: str) -> UUID:
        return uuid5(UUID(int=seed), f"tx-{name}")

    gt_transfers, ground_truth_rings = _generate_ground_truth_rings(now, node_id, tx_id)
    distractor_transfers = _generate_distractor_transfers(now, node_id, tx_id)
    all_transfers = gt_transfers + distractor_transfers
    return all_transfers, ground_truth_rings


def evaluate_circular_detection(
    transfers: Sequence[Any],
    ground_truth_rings: set[frozenset[UUID]],
    *,
    lookback_window: timedelta = timedelta(days=14),
    as_of: datetime | None = None,
    min_length: int = DEFAULT_MIN_CYCLE_LENGTH,
    max_length: int = DEFAULT_MAX_CYCLE_LENGTH,
    min_retention: float = DEFAULT_MIN_RETENTION,
) -> dict[str, Any]:
    """Evaluate circular loop detection against known ground truth."""
    graph = build_transaction_graph(transfers, lookback_window=lookback_window, as_of=as_of)
    detected_loops = detect_circular_loops(
        graph,
        min_length=min_length,
        max_length=max_length,
        min_retention=min_retention,
    )

    detected_sets = {detected.node_set for detected in detected_loops}
    tp_sets = detected_sets & ground_truth_rings
    fp_sets = detected_sets - ground_truth_rings
    fn_sets = ground_truth_rings - detected_sets

    tp = len(tp_sets)
    fp = len(fp_sets)
    fn = len(fn_sets)

    precision = float(tp) / float(tp + fp) if (tp + fp) > 0 else 1.0
    recall = float(tp) / float(tp + fn) if (tp + fn) > 0 else 1.0

    return {
        "transfers_count": len(transfers),
        "graph_nodes": graph.number_of_nodes(),
        "graph_edges": graph.number_of_edges(),
        "ground_truth_count": len(ground_truth_rings),
        "detected_count": len(detected_loops),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "detected_loops": detected_loops,
    }
