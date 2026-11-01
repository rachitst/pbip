# Spike: circular transfers / mule rings (F11, TASK-072)

Status: **spike, not in production paths.** Nothing on the API request path, the ledger, the payment
services or the fraud scorer imports `pbip_ml.fraud.circular`. This page records what was built,
how it was measured, what the numbers mean, and what we recommend.

## Question

Can we find **circular fund loops** (A -> B -> C -> A), the pattern a mule ring uses to layer money,
with NetworkX, at our data size, and with what false-positive risk?

A loop is reported only if **all three** conditions hold:

| Condition | Default | Where | Test that fails without it |
|---|---|---|---|
| Length 3 to 5 accounts | `min_length=3`, `max_length=5` | `detect_circular_loops` (NetworkX `length_bound` + filter) | `test_detect_cycles_rejects_lengths_1_2_and_6`, `test_two_cycle_inside_a_larger_graph_is_ignored`, `test_cycle_enumeration_is_length_bounded` |
| More than 90% of the funds retained around the loop | `min_retention=0.90`, strict `>` | `_check_retention` (exact integer comparison) | `test_retention_exactly_90_is_rejected`, `test_loop_leaking_more_than_ten_percent_is_ignored`, `test_strict_integer_retention_boundary_paise_precision` |
| Bounded time window | loop closes within 14 days (`max_loop_span`), each hop within 7 days and forward in time (`max_hop_delay`), graph built from a 14-day lookback | `_find_single_tx_causal_path`, `_find_bundle_causal_flow`, `build_transaction_graph` | `test_loop_closing_outside_the_time_window_is_ignored`, `test_loop_window_also_bounds_bundled_legs`, `test_adversarial_rejected_*`, `test_build_graph_lookback_window_filtering` |

## Method

1. **Graph** (`build_transaction_graph`): nodes are accounts, a directed edge u -> v carries every
   transfer from u to v inside the lookback window (amounts in integer paise, replayed ids ignored).
   Rows are typed structurally (`TransferLike`); `pbip_datagen.fraud.Transfer` fits as is.
2. **Enumeration**: `nx.simple_cycles(graph, length_bound=5)`. The bound matters: unbounded
   enumeration is exponential (see Runtime).
3. **Causal flow check** for each cycle and each starting point: either one transfer per leg moving
   forward in time (best retention wins; partial paths that already fail retention, hop delay or
   span are pruned), or whole legs where each leg finishes before the next starts.
   Retention = smallest leg amount / largest leg amount on that path.
4. **Output**: `calculate_risk_boosts` gives each participating account a flat +35 (capped at 100)
   with human-readable reasons and the transfer ids as evidence. It is an *output of the spike*,
   not a scorer input (see Recommendation).

## Data

* **Hand-built benchmark** (`scripts/evaluate_circular_rings.py benchmark`): 58 transfers, 4
  planted rings (lengths 3, 4, 5; 92.5-96% retained) and 9 distractors (50% retention, exact 90%,
  2-cycle, 6-cycle, outside the window, a chain, a salary fan-out, time-reversed loop, scattered
  legs with equal totals).
* **Generator size** (`scripts/evaluate_circular_rings.py generator`): the same transfers as
  `make datagen` (seed 42, 1,000 customers, 12 months), built in memory, no database:
  **34,059 transfers**. Rings are **planted in the script, additively**; `pbip_datagen` and the
  training data are unchanged, so F1-F6/L1-L3 outputs and the trained models are unaffected.
  * 30 positive rings (10 each of length 3, 4, 5) between distinct existing customers, start
    INR 50k-5L, 0.5-2% lost per hop, hops 1-36 h apart.
  * 50 negative rings the detector must ignore (10 each): leaky (77-88% retained), 2-cycles,
    6-cycles, slow (hops 3.5-4.5 days apart, so each hop is within 7 days but the loop takes more
    than 14), time-reversed.
  * Detection runs as a **weekly batch**: a 14-day lookback graph every 7 days (51 windows),
    alerts unioned.
  * **Stress variant**: every ordinary transfer is rewired to a random other customer, so the
    graph has customer-to-customer edges and coincidental cycles (pessimistic for false positives).

## Results (generator v2.2 with F7/F8, run 2026-11-01, shared 2-CPU / 7 GB machine under load from other jobs)

| Run | Detected / planted | False positives | Negatives flagged | Time (51 windows) | Slowest window | Memory |
|---|---|---|---|---|---|---|
| Hand-built benchmark | 4 / 4 | 0 | 0 / 9 distractors | 16 ms | - | - |
| Generator data, untouched | - | **0** | - | 10.4 s CPU (13.1 s wall) | 517 ms | - |
| Generator data + planted rings | **30 / 30** | **0** | **0 / 50** | 49.2 s CPU (67 s wall) with tracemalloc on | 2.3 s | tracemalloc peak 7.5 MiB |
| Stress: rewired to customers + planted | 30 / 30 | 0 | 0 / 50 | 13.8 s CPU (25.5 s wall) | 1.0 s | - |

Whole script: 146 s wall, **peak RSS 96 MiB**. Largest window graph: about 1,970 nodes and 1,212
edges (rewired: 955 nodes, 1,553 edges, at most 14 cycles of length <= 5 per window). Most of the
per-window time is rebuilding the graph from all 34k rows (about 144 ms CPU per window on the
untouched data) against about 16 ms for detection; a real job would build the window incrementally.

**Detection rate 100% (30/30), precision 100%, 0 of 50 near-miss negatives flagged.**

### Cycle enumeration is bounded

One-off measurement on random directed graphs with 1,000 nodes (seed 42):

| Average out-degree | `simple_cycles(length_bound=5)` | Unbounded `simple_cycles` |
|---|---|---|
| 4 | 316 cycles, 3.0 s CPU | > 1.1 million cycles after 30 s CPU, longest 648 nodes, still running |
| 8 | 7,649 cycles, 9.3 s CPU | > 1.0 million cycles after 30 s CPU, longest 818 nodes, still running |

The PR originally called `simple_cycles(graph)` without a bound and filtered afterwards, which
would never finish on a dense graph. `test_cycle_enumeration_is_length_bounded` now fails if the
bound is removed.

## Limitations (read before quoting the numbers)

* **The real generator data has no customer-to-customer transfers.** Every counterparty is a
  per-customer synthetic payee, so the untouched graph is a forest of stars with **zero**
  customer-to-customer edges and therefore zero cycles. "0 false positives on normal data" is
  true but says almost nothing; the stress variant is the more informative false-positive check,
  and even that is sparse (14-day windows hold about 1.5 transfers per customer).
* **The planted rings were written by the same people who wrote the detector**, with the same
  definition (3-5 hops, > 90%, short hops). 100% detection is a consistency check, not evidence
  of real-world recall. Real rings add noise: partial forwarding, splits/merges across several
  accounts, cash-out legs, delays tuned to evade a known window, and legs through other banks we
  never see.
* **Real legitimate loops exist** (shared expenses, family transfers, business sweeps). We have
  no labelled data for them; the false-positive rate on real traffic is unknown.
* **Fungibility**: balances are fungible, so the check shows money *could* have gone around the
  loop (time order and amounts fit), not that it *did*.
* **Worst case of the single-transfer path search**: pruning keeps it small in practice, but many
  near-equal transfers on every leg inside the window can still multiply paths. A production job
  would cap transfers per edge or switch to the leg-aggregate check for busy edges.
* Timings come from a loaded shared machine; treat them as order of magnitude.

## Recommendation

1. **Do not wire this into the online scorer or the payment path.** It needs a graph over many
   customers' history, is batch by nature, and a +35 boost from an unvalidated detector would add
   unexplained holds. The +35 stays an output of the spike.
2. If F11 moves from COULD to SHOULD: run it as a **nightly/weekly offline batch** that raises
   analyst *cases* (with the loop, amounts and transfer ids as evidence), not automatic holds;
   that needs an ADR (new background job, new table for cases) per AGENTS.md rule 5.
3. **Before trusting any rate:** extend the generator with real customer-to-customer P2P traffic
   (including legitimate loops) and a labelled ring injector, as a separate additive scenario with
   its own seed stream so existing F1-F8/L1-L3 output stays byte-identical, then re-evaluate here.
   The current fraud models do not use this signal and need no retraining.
4. Cost is not a blocker: at our size a weekly run is seconds of CPU and under 100 MiB.

## Reproduce

```bash
uv run pytest ml/tests/test_fraud_circular.py -q
uv run python scripts/evaluate_circular_rings.py benchmark
uv run python scripts/evaluate_circular_rings.py generator --json /tmp/f11.json  # about 2-3 min
```
