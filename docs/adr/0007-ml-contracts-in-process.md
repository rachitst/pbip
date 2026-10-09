# ADR 0007: ML behind versioned contracts, executed in-process
- Status: Accepted
- Date: 2026-10-07
- Deciders: A, B

## Context
The platform and intelligence lanes must work in parallel and the fraud decision must stay explainable and fast.

## Decision
`pbip_ml.<area>.contract` modules (Pydantic + Protocol) define inputs/outputs; the API imports only contracts and inference; training is offline; models load from the MLflow registry at start with local fallback.

## Alternatives considered
Separate model-serving service - adds latency, deployment and auth surface; direct DB access from notebooks into API code - tight coupling.

## Consequences
Parallel work via test doubles; sub-100 ms scoring. Contract changes need an ADR.
