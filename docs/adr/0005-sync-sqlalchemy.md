# ADR 0005: Synchronous SQLAlchemy 2.0
- Status: Accepted
- Date: 2026-10-07
- Deciders: Architecture Team

## Context
Ledger correctness and lock reasoning matter more than raw async throughput; Celery and scripts share the same code.

## Decision
Sync SQLAlchemy + psycopg3; FastAPI runs sync routes in a threadpool; one uvicorn worker per container, scale by replicas.

## Alternatives considered
Async SQLAlchemy - more failure modes around locks/transactions and testing for little benefit at our scale.

## Consequences
Simpler mental model and tests. Revisit only if profiling (k6) shows a bottleneck.
