# ADR 0001: Modular monolith, not microservices
- Status: Accepted
- Date: 2026-10-07
- Deciders: Architecture Team

## Context
The project is dominated by integration cost; ML and API should share one language and deployment.

## Decision
One FastAPI deployable with strict module boundaries (import-linter) and stateless containers. ML packaged in-process behind contracts.

## Alternatives considered
Microservices - network/consistency/ops overhead we cannot afford; Node API + Python ML sidecar - two runtimes, duplicated auth.

## Consequences
Simple deploys and debugging; boundaries enforced by tooling keep a later split possible (fraud scorer is already an interface). Must resist cross-module imports.
