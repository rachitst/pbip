# ADR 0010: Draft customer feature contract is point-in-time by construction
- Status: Accepted (contract itself is a draft: `customer-features-v0.1-draft`)
- Date: 2026-10-25
- Deciders: Architecture Team

## Context
TASK-010 drafts behavioural features (`avg_amount_30d`, `transfer_velocity_10m`, ...) for the fraud
engine and analytics, and must validate the absence of target leakage. `pbip_ml.*.contract`
modules are cross-domain contracts (AGENTS section 2): the platform will compute these features online
(Sprint 2) and the ML side trains on the offline equivalent; any mismatch is train/serve skew.

## Decision
- `pbip_ml.features.contract`: strict, frozen Pydantic models with `extra="forbid"` -
  `TransactionEvent` (the only feature input; it has no label fields, so labels cannot be passed in),
  `CustomerFeatures` (versioned, money in integer minor units) and a `FEATURE_CATALOGUE` documenting
  every feature's window, unit and purpose.
- `pbip_ml.features.offline.compute_customer_features(customer_id, events, as_of)` is the reference
  implementation and the parity oracle for the online pipeline. It drops every event at or after
  `as_of` before computing anything.
- Leakage is tested, not asserted: a Hypothesis property shows that appending any events at/after
  `as_of` never changes the vector, and the EDA notebook demonstrates the bias a naive
  implementation would introduce.

## Alternatives considered
- Plain dicts / pandas columns: no validation, silent schema drift between training and serving.
- A feature store product (Feast): heavy for the project scale; revisit if feature count grows.

## Consequences
Not consumed by the API yet (no request-path change). Changing a field is a contract change: bump
`FEATURE_SET_VERSION` and record it here. Known gap from the EDA: data-gen v1 has no night-time
activity, so `night_debit_ratio_30d` is constant until data-gen v2.
