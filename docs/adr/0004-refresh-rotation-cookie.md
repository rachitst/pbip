# ADR 0004: Rotating refresh tokens in HttpOnly cookies; access token in memory
- Status: Accepted
- Date: 2026-10-07
- Deciders: A, B

## Context
Tokens in localStorage are exfiltrable by XSS; long-lived JWTs cannot be revoked.

## Decision
15-minute access JWT held in memory; opaque refresh token (hashed in DB) in an HttpOnly SameSite=Strict cookie, rotated on every use with family-wide revocation on reuse; CSRF guard header + Origin check on cookie endpoints.

## Alternatives considered
JWT in localStorage - XSS risk; server sessions only - fine, but loses stateless API and the explainable rotation story.

## Consequences
Theft containment and good security narrative. Concurrent refresh with the same token is treated as reuse (documented).
