"""Graph-based circular transfer and mule ring detection spike (TASK-072, F11).

Detects circular fund loops between mule accounts using directed transaction graphs
and NetworkX. Evaluates simple directed cycles of length 3-5 with high funds retention
(strictly > 90%), verifying causal temporal progression and flow compatibility.

Architecture:
- Pure graph analysis operating on directed fund movements.
- Bounded to `pbip_ml.fraud` without depending on the API monolith (import-linter contract).
- Deterministic, side-effect free, and auditable.

Limitation & Provenance Notice:
In retail banking, account balances are fungible liabilities. The Transfer contract does not
carry cryptographic coin provenance or parent transaction IDs linking incoming credits to
outgoing debits. This module implements Causal Temporal Progression and Flow Compatibility
(CTP-FC): it verifies that funds could causally circulate by checking strict forward temporal
ordering (t_in <= t_out across consecutive hops), maximum hop delays, and amount retention
compatibility across the cycle. It does not prove that funds were not commingled with other
account balance.

Offline spike only: nothing on the API request path, the ledger or the fraud scorer imports this
module (results and recommendation: ``docs/ml/circular-transfer-spike.md``). Input rows are typed
structurally (``TransferLike``) so ``pbip_ml`` does not import ``pbip_datagen`` at runtime.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Protocol
from uuid import UUID, uuid5

import networkx as nx

DEFAULT_MIN_CYCLE_LENGTH = 3
DEFAULT_MAX_CYCLE_LENGTH = 5
DEFAULT_MIN_RETENTION = 0.90
DEFAULT_MAX_HOP_DELAY = timedelta(days=7)
# The whole loop (first hop to last hop) must close inside this window; equal to the default
# lookback so a loop can never be assembled from transfers further apart than one batch window.
DEFAULT_MAX_LOOP_SPAN = timedelta(days=14)
DEFAULT_RISK_BOOST = 35
MAX_SCORE = 100


class TransferLike(Protocol):
    """The fields this module reads from a transfer row (``pbip_datagen.fraud.Transfer`` fits)."""

    @property
    def id(self) -> UUID: ...
    @property
    def customer_id(self) -> UUID: ...
    @property
    def counterparty_id(self) -> UUID: ...
    @property
    def direction(self) -> str: ...
    @property
    def amount_minor(self) -> int: ...
    @property
    def occurred_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class TransferRow:
    """A minimal concrete transfer, used by the synthetic benchmark below."""

    id: UUID
    customer_id: UUID
    counterparty_id: UUID
    direction: str
    amount_minor: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class DetectionReport:
    """Ground-truth comparison of detected loops (node sets) against planted rings."""

    transfers_count: int
    graph_nodes: int
    graph_edges: int
    ground_truth_count: int
    detected_count: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    detected_loops: tuple["CircularLoop", ...]


@dataclass(frozen=True, slots=True)
class EdgeTransfer:
    """Individual transfer recorded along a directed graph edge."""

    id: UUID
    amount_minor: int
    occurred_at: datetime


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
class CausalFlowResult:
    """Evidence of causally ordered, amount-compatible fund movement around a cycle."""

    is_valid: bool
    retention_ratio: float
    bottleneck_minor: int
    peak_minor: int
    start_node: UUID
    chronological_tx_ids: tuple[UUID, ...]
    hop_timestamps: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class CircularLoop:
    """A detected simple cycle in the transaction graph satisfying causal flow and retention."""

    nodes: tuple[UUID, ...]
    length: int
    retention_ratio: float
    bottleneck_minor: int
    peak_minor: int
    transaction_ids: tuple[UUID, ...]
    edges: tuple[CycleEdge, ...]
    chronological_start_node: UUID
    hop_timestamps: tuple[datetime, ...]

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


def _ensure_utc(dt: datetime) -> datetime:
    """Normalise an aware datetime to UTC; naive datetimes are ambiguous and rejected."""
    if dt.tzinfo is None:
        raise ValueError("transfer timestamps must be timezone-aware")
    return dt.astimezone(UTC)


def _check_retention(
    b_min: int,
    p_max: int,
    min_retention: float = DEFAULT_MIN_RETENTION,
) -> tuple[bool, float]:
    """Return (b_min / p_max > min_retention, ratio) with the comparison done exactly.

    The threshold is read as its decimal literal (0.90 -> 9/10) and compared by integer
    cross-multiplication, so a boundary loop is never accepted or rejected by float rounding.
    """
    if p_max <= 0:
        return False, 0.0
    threshold = Fraction(str(min_retention))
    is_above = b_min * threshold.denominator > p_max * threshold.numerator
    return is_above, b_min / p_max


def _extract_transfer_tuple(
    record: TransferLike,
) -> tuple[UUID, UUID, int, datetime, UUID]:
    """Return (source, target, amount_minor, occurred_at, tx_id): money flows source -> target."""
    occurred_at = _ensure_utc(record.occurred_at)
    if record.direction == "DEBIT":
        src, tgt = record.customer_id, record.counterparty_id
    elif record.direction == "CREDIT":
        src, tgt = record.counterparty_id, record.customer_id
    else:
        raise ValueError("transfer direction must be DEBIT or CREDIT")
    return (src, tgt, int(record.amount_minor), occurred_at, record.id)


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
    """Add or accumulate a directed transaction along edge src -> tgt with replay prevention."""
    tx_entry = EdgeTransfer(id=tx_id, amount_minor=amt, occurred_at=occurred_at)
    if graph.has_edge(src, tgt):
        edge_data = graph[src][tgt]
        if tx_id in edge_data["transaction_ids"]:
            return  # Duplicate transaction replay protection
        edge_data["amount_minor"] += amt
        edge_data["transaction_ids"].append(tx_id)
        edge_data["transfers"].append(tx_entry)
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
            transfers=[tx_entry],
            first_occurred_at=occurred_at,
            last_occurred_at=occurred_at,
        )


def build_transaction_graph(
    transfers: Iterable[TransferLike],
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
        if extracted[2] > 0 and extracted[0] != extracted[1]:
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


_PathResult = tuple[float, int, int, list[UUID], list[datetime]]


def _find_single_tx_causal_path(
    leg_transfers: list[list[EdgeTransfer]],
    max_hop_delay: timedelta,
    min_retention: float,
    max_loop_span: timedelta,
) -> _PathResult | None:
    """Best single-transfer-per-leg path moving forward in time across the legs.

    Search is pruned as soon as a partial path breaks the hop delay, the loop span or the
    retention threshold: min only falls and max only rises as hops are added, so a failing
    partial ratio can never recover. This keeps the search small even for busy edges.
    """
    best: list[_PathResult] = []
    chosen: list[EdgeTransfer] = []

    def admissible(tx: EdgeTransfer) -> bool:
        if not chosen:
            return True
        prev, first = chosen[-1].occurred_at, chosen[0].occurred_at
        return (
            prev <= tx.occurred_at
            and tx.occurred_at - prev <= max_hop_delay
            and tx.occurred_at - first <= max_loop_span
        )

    def dfs(step: int, lo: int, hi: int) -> None:
        if step == len(leg_transfers):
            ratio = lo / hi
            if not best or ratio > best[0][0]:
                ids = [tx.id for tx in chosen]
                best[:] = [(ratio, lo, hi, ids, [tx.occurred_at for tx in chosen])]
            return
        for tx in leg_transfers[step]:
            new_lo, new_hi = min(lo, tx.amount_minor), max(hi, tx.amount_minor)
            if admissible(tx) and _check_retention(new_lo, new_hi, min_retention)[0]:
                chosen.append(tx)
                dfs(step + 1, new_lo, new_hi)
                chosen.pop()

    for first_tx in leg_transfers[0] if leg_transfers else []:
        chosen.append(first_tx)
        dfs(1, first_tx.amount_minor, first_tx.amount_minor)
        chosen.pop()
    return best[0] if best else None


def _find_bundle_causal_flow(
    legs: list[tuple[UUID, UUID]],
    graph: nx.DiGraph,
    max_hop_delay: timedelta,
    min_retention: float,
    max_loop_span: timedelta,
) -> _PathResult | None:
    """Verify aggregated leg bundling where each entire leg completes before next leg starts."""
    leg_totals: list[int] = []
    leg_starts: list[datetime] = []
    leg_ends: list[datetime] = []
    all_tx_ids: list[UUID] = []

    for u, v in legs:
        edge = graph[u][v]
        txs: list[EdgeTransfer] = edge["transfers"]
        if not txs:
            return None
        leg_totals.append(int(edge["amount_minor"]))
        leg_starts.append(edge["first_occurred_at"])
        leg_ends.append(edge["last_occurred_at"])
        all_tx_ids.extend(edge["transaction_ids"])

    # Check that leg j completes at or before leg j+1 starts
    for j in range(len(legs) - 1):
        if leg_ends[j] > leg_starts[j + 1]:
            return None
        if leg_starts[j + 1] - leg_ends[j] > max_hop_delay:
            return None
    if leg_ends[-1] - leg_starts[0] > max_loop_span:
        return None

    b_min = min(leg_totals)
    p_max = max(leg_totals)
    is_above, ret = _check_retention(b_min, p_max, min_retention)
    if not is_above:
        return None

    return (ret, b_min, p_max, all_tx_ids, leg_starts)


def _verify_rotation(
    legs: list[tuple[UUID, UUID]],
    graph: nx.DiGraph,
    max_hop_delay: timedelta,
    min_retention: float,
    max_loop_span: timedelta,
) -> _PathResult | None:
    """Evaluate both single-transfer and bundled multi-transfer flows for a rotation."""
    leg_transfers: list[list[EdgeTransfer]] = [
        sorted(graph[u][v]["transfers"], key=lambda tx: (tx.occurred_at, tx.id)) for u, v in legs
    ]

    single_res = _find_single_tx_causal_path(
        leg_transfers, max_hop_delay, min_retention, max_loop_span
    )
    bundle_res = _find_bundle_causal_flow(legs, graph, max_hop_delay, min_retention, max_loop_span)

    if single_res is None:
        return bundle_res
    if bundle_res is None:
        return single_res

    return single_res if single_res[0] >= bundle_res[0] else bundle_res


def verify_causal_cycle_flow(
    cycle: Sequence[UUID],
    graph: nx.DiGraph,
    *,
    min_retention: float = DEFAULT_MIN_RETENTION,
    max_hop_delay: timedelta = DEFAULT_MAX_HOP_DELAY,
    max_loop_span: timedelta = DEFAULT_MAX_LOOP_SPAN,
) -> CausalFlowResult | None:
    """Verify that a cycle exhibits forward chronological progression with high retention.

    Tests all cycle rotations to determine if funds could causally circulate around the ring.
    """
    n_nodes = len(cycle)
    if n_nodes < 2:
        return None

    cycle_edges = [(cycle[i], cycle[(i + 1) % n_nodes]) for i in range(n_nodes)]
    for u, v in cycle_edges:
        if not graph.has_edge(u, v):
            return None

    best_flow: tuple[float, int, int, list[UUID], list[datetime], UUID] | None = None
    best_ret = 0.0

    for start_idx in range(n_nodes):
        rotated_legs = [cycle_edges[(start_idx + j) % n_nodes] for j in range(n_nodes)]
        res = _verify_rotation(rotated_legs, graph, max_hop_delay, min_retention, max_loop_span)
        if res is not None and res[0] > best_ret:
            best_ret = res[0]
            start_node = cycle[start_idx]
            best_flow = (res[0], res[1], res[2], res[3], res[4], start_node)

    if best_flow is None:
        return None

    return CausalFlowResult(
        is_valid=True,
        retention_ratio=best_flow[0],
        bottleneck_minor=best_flow[1],
        peak_minor=best_flow[2],
        start_node=best_flow[5],
        chronological_tx_ids=tuple(best_flow[3]),
        hop_timestamps=tuple(best_flow[4]),
    )


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
    max_hop_delay: timedelta = DEFAULT_MAX_HOP_DELAY,
    max_loop_span: timedelta = DEFAULT_MAX_LOOP_SPAN,
) -> list[CircularLoop]:
    """Detect simple directed cycles of length 3-5 with verified causal flow and retention > 90%.

    A loop qualifies only if all three hold: ``min_length <= len <= max_length``, the funds
    retained around it exceed ``min_retention`` and it closes within ``max_loop_span`` (each hop
    also within ``max_hop_delay``). Enumeration passes ``length_bound`` to NetworkX so cycles
    longer than ``max_length`` are never generated: unbounded ``simple_cycles`` is exponential.
    """
    if graph.number_of_nodes() < min_length or graph.number_of_edges() < min_length:
        return []

    seen_cycles: set[tuple[UUID, ...]] = set()
    qualifying_loops: list[CircularLoop] = []

    # NetworkX simple_cycles enumerates elementary circuits
    for raw_cycle in nx.simple_cycles(graph, length_bound=max_length):
        cycle_len = len(raw_cycle)
        if cycle_len < min_length or cycle_len > max_length:
            continue

        normalized = _normalize_cycle(raw_cycle)
        if normalized in seen_cycles:
            continue
        seen_cycles.add(normalized)

        # Causal temporal progression and amount compatibility verification
        causal_flow = verify_causal_cycle_flow(
            normalized,
            graph,
            min_retention=min_retention,
            max_hop_delay=max_hop_delay,
            max_loop_span=max_loop_span,
        )
        if causal_flow is None:
            continue  # Broken causality or insufficient retention

        edge_details = _build_cycle_edges(normalized, graph)

        qualifying_loops.append(
            CircularLoop(
                nodes=normalized,
                length=cycle_len,
                retention_ratio=causal_flow.retention_ratio,
                bottleneck_minor=causal_flow.bottleneck_minor,
                peak_minor=causal_flow.peak_minor,
                transaction_ids=causal_flow.chronological_tx_ids,
                edges=edge_details,
                chronological_start_node=causal_flow.start_node,
                hop_timestamps=causal_flow.hop_timestamps,
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


def _make_synth_transfer(
    tx_id: UUID,
    src: UUID,
    tgt: UUID,
    amount_minor: int,
    occurred_at: datetime,
) -> TransferRow:
    """A DEBIT from ``src`` to ``tgt`` for the synthetic benchmark."""
    return TransferRow(
        id=tx_id,
        customer_id=src,
        counterparty_id=tgt,
        direction="DEBIT",
        amount_minor=amount_minor,
        occurred_at=occurred_at,
    )


def _generate_ground_truth_rings(
    now: datetime,
    node_id_fn: Callable[[str], UUID],
    tx_id_fn: Callable[[str], UUID],
) -> tuple[list[TransferRow], set[frozenset[UUID]]]:
    """Generate ground truth circular rings with strictly forward chronological ordering."""
    transfers: list[TransferRow] = []
    ground_truth_rings: set[frozenset[UUID]] = set()

    # Ring 1: 3-Node Ring (96% retention, forward hops: +3h each)
    r1_nodes = [node_id_fn(f"r1_{i}") for i in range(3)]
    ground_truth_rings.add(frozenset(r1_nodes))
    r1_amounts = [10_000_000, 9_800_000, 9_600_000]
    r1_base = now - timedelta(days=4)
    for i in range(3):
        t_time = r1_base + timedelta(hours=3 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r1_leg_{i}"),
                r1_nodes[i],
                r1_nodes[(i + 1) % 3],
                r1_amounts[i],
                t_time,
            )
        )

    # Ring 2: 4-Node Ring (94% retention, forward hops: +2h each)
    r2_nodes = [node_id_fn(f"r2_{i}") for i in range(4)]
    ground_truth_rings.add(frozenset(r2_nodes))
    r2_amounts = [5_000_000, 4_900_000, 4_800_000, 4_700_000]
    r2_base = now - timedelta(days=5)
    for i in range(4):
        t_time = r2_base + timedelta(hours=2 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r2_leg_{i}"),
                r2_nodes[i],
                r2_nodes[(i + 1) % 4],
                r2_amounts[i],
                t_time,
            )
        )

    # Ring 3: 5-Node Ring (92.5% retention, forward hops: +4h each)
    r3_nodes = [node_id_fn(f"r3_{i}") for i in range(5)]
    ground_truth_rings.add(frozenset(r3_nodes))
    r3_amounts = [20_000_000, 19_500_000, 19_000_000, 18_800_000, 18_500_000]
    r3_base = now - timedelta(days=6)
    for i in range(5):
        t_time = r3_base + timedelta(hours=4 * i)
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"r3_leg_{i}"),
                r3_nodes[i],
                r3_nodes[(i + 1) % 5],
                r3_amounts[i],
                t_time,
            )
        )

    # Ring 4: 3-Node Ring with multiple transfers on leg 0 (95% retention, forward causal order)
    r4_nodes = [node_id_fn(f"r4_{i}") for i in range(3)]
    ground_truth_rings.add(frozenset(r4_nodes))
    r4_base = now - timedelta(days=2)
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_0a"),
            r4_nodes[0],
            r4_nodes[1],
            6_000_000,
            r4_base + timedelta(hours=1),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_0b"),
            r4_nodes[0],
            r4_nodes[1],
            4_000_000,
            r4_base + timedelta(hours=2),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_1"),
            r4_nodes[1],
            r4_nodes[2],
            9_700_000,
            r4_base + timedelta(hours=5),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("r4_leg_2"),
            r4_nodes[2],
            r4_nodes[0],
            9_500_000,
            r4_base + timedelta(hours=8),
        )
    )

    return transfers, ground_truth_rings


def _generate_distractor_transfers(
    now: datetime,
    node_id_fn: Callable[[str], UUID],
    tx_id_fn: Callable[[str], UUID],
) -> list[TransferRow]:
    """Generate distractor transaction structures including adversarial negative cases."""
    transfers: list[TransferRow] = []

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
                now - timedelta(days=2) + timedelta(hours=2 * i),
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
                now - timedelta(days=2) + timedelta(hours=2 * i),
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
                now - timedelta(days=3) + timedelta(hours=i),
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
                now - timedelta(days=45) + timedelta(hours=i),
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
                now - timedelta(days=1) + timedelta(hours=i),
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

    # Distractor 8 (Adversarial): Reversed chronological order (broken causality)
    # Total volume on each leg is 10,000,000, but timestamps flow BACKWARDS in time!
    d8_nodes = [node_id_fn(f"d8_{i}") for i in range(3)]
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d8_leg_0"), d8_nodes[0], d8_nodes[1], 10_000_000, now - timedelta(days=1)
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d8_leg_1"), d8_nodes[1], d8_nodes[2], 10_000_000, now - timedelta(days=5)
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d8_leg_2"), d8_nodes[2], d8_nodes[0], 10_000_000, now - timedelta(days=10)
        )
    )

    # Distractor 9 (Adversarial): Unrelated parallel transactions with similar total volume
    # Leg 0 has 10 transfers of 1,000,000 (total 10,000,000)
    # Leg 1 has 1 transfer of 10,000,000
    # Leg 2 has 2 transfers of 5,000,000
    # Amounts per individual transfer do not match across legs (ratio 10% on single-tx path)
    # and timestamps on Leg 0 are scattered after Leg 1.
    d9_nodes = [node_id_fn(f"d9_{i}") for i in range(3)]
    for i in range(10):
        transfers.append(
            _make_synth_transfer(
                tx_id_fn(f"d9_leg_0_{i}"),
                d9_nodes[0],
                d9_nodes[1],
                1_000_000,
                now - timedelta(days=2) + timedelta(hours=i),
            )
        )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d9_leg_1"),
            d9_nodes[1],
            d9_nodes[2],
            10_000_000,
            now - timedelta(days=2, hours=5),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d9_leg_2_0"),
            d9_nodes[2],
            d9_nodes[0],
            5_000_000,
            now - timedelta(days=3),
        )
    )
    transfers.append(
        _make_synth_transfer(
            tx_id_fn("d9_leg_2_1"),
            d9_nodes[2],
            d9_nodes[0],
            5_000_000,
            now - timedelta(days=3, hours=2),
        )
    )

    return transfers


def generate_circular_ring_evaluation_dataset(
    seed: int = 42,
    base_time: datetime | None = None,
) -> tuple[list[TransferRow], set[frozenset[UUID]]]:
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
    transfers: Sequence[TransferLike],
    ground_truth_rings: set[frozenset[UUID]],
    *,
    lookback_window: timedelta = timedelta(days=14),
    as_of: datetime | None = None,
    min_length: int = DEFAULT_MIN_CYCLE_LENGTH,
    max_length: int = DEFAULT_MAX_CYCLE_LENGTH,
    min_retention: float = DEFAULT_MIN_RETENTION,
    max_hop_delay: timedelta = DEFAULT_MAX_HOP_DELAY,
    max_loop_span: timedelta = DEFAULT_MAX_LOOP_SPAN,
) -> DetectionReport:
    """Evaluate circular loop detection against known ground truth."""
    graph = build_transaction_graph(transfers, lookback_window=lookback_window, as_of=as_of)
    detected_loops = detect_circular_loops(
        graph,
        min_length=min_length,
        max_length=max_length,
        min_retention=min_retention,
        max_hop_delay=max_hop_delay,
        max_loop_span=max_loop_span,
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

    return DetectionReport(
        transfers_count=len(transfers),
        graph_nodes=graph.number_of_nodes(),
        graph_edges=graph.number_of_edges(),
        ground_truth_count=len(ground_truth_rings),
        detected_count=len(detected_loops),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        detected_loops=tuple(detected_loops),
    )
