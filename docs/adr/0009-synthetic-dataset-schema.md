# ADR 0009: Synthetic training data lives in a separate `synthetic` schema, loaded with COPY
- Status: Accepted
- Date: 2026-10-25
- Deciders: Architecture Team

## Context
TASK-009 needs a deterministic synthetic dataset (personas, salaries, EMIs, bills, spending) and a
PostgreSQL COPY bulk load of 1,000 customers in under 15 s. The operational ledger may only be
written by `ledger/service.py`, one balanced transaction at a time under row locks; bulk-copying
~400k rows into `transaction_entries` would bypass every ledger invariant. The data is offline ML
training/analysis material, consumed by notebooks and (Sprint 2) the feature pipeline, never by
customer-facing endpoints. `pbip_datagen` must not import `pbip_api`.

## Decision
- A dedicated `synthetic` schema (migration `0002`): `generation_runs`, `customers`, `transactions`.
  No ORM models; `alembic check` compares the default schema only.
- `pbip_datagen.loader` writes it with binary COPY in one transaction. A run's id is derived from
  `(generator version, seed, config)`, so reloading replaces the run (idempotent).
- Integrity is DB-enforced but set-based. Measured: two row-level FKs on `transactions` took
  ~85% of COPY time (~23 s of ~27 s). Instead, statement-level triggers with transition tables
  reject any insert whose `(customer_id, run_id)` has no matching customer, cascade customer
  deletes, and make rows immutable (a run is replaced, never edited). `TRUNCATE customers` is
  refused while transactions exist. `customers.run_id` keeps an ordinary FK (1 row per customer).
  The insert check first takes `FOR KEY SHARE` on the referenced customers - the lock a real FK
  would take - so a concurrent run deletion cannot commit orphans under READ COMMITTED.
- `ANALYZE synthetic.customers` before copying transactions, so the integrity check hash-joins
  (a never-analysed table planned a nested loop: 29 s vs 4.6 s).

## Alternatives considered
- Load into the operational tables via the ledger service: correct but ~400k locked postings -
  minutes, not seconds - and pollutes the live bank with fake customers.
- Row-level FKs: simplest, but misses the 15 s budget.
- Drop/re-add FKs around the load: DDL outside migrations and needs table ownership at runtime.
- Parquet/CSV only: no SQL access for EDA/feature work; COPY is an explicit acceptance criterion.

## Consequences
Measured locally (Ubuntu, PostgreSQL 16 in Docker): COPY 4.5-6.2 s for 1,000 customers /
~390k transactions; 10.7-12.3 s including generation. Fraud labels are not part of v1 (data-gen
v2 adds them by a new migration). Persona/event CHECK lists must be migrated when personas change.
