# ADR 0006: First-party GitHub Actions and locked scanners
- Status: Accepted
- Date: 2026-10-07
- Deciders: Architecture Team

## Context
Mutable third-party action tags have been used for supply-chain attacks (a popular scanner's action was reported compromised in March 2026).

## Decision
CI uses only `actions/*`, `docker/*`, `github/*`; scanners install from lockfile-pinned packages; any third-party action must be pinned to a full commit SHA; Dependabot keeps pins fresh.

## Alternatives considered
Trivy/Gitleaks actions on mutable tags - convenient but a pipeline-takeover risk.

## Consequences
Slightly less tooling out of the box (add Trivy/Sonar later with verified, pinned releases). Smaller attack surface.
