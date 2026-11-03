# ADR 0044: A signed-out refresh and a too-new forecast are answers, not HTTP errors
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team (issue #382, TASK-089 criterion "zero console errors")

## Context
The TASK-089 crawl found two browser console errors ("Failed to load resource: ... 4xx") on pages that work
correctly. The browser logs every failed fetch, and JavaScript cannot suppress that, so the web app cannot fix them
by handling the error more quietly:

1. `POST /api/v1/auth/refresh` answered **401** on every page load without a session. The web app asks once on
   load to restore a session from the HttpOnly refresh cookie; with no cookie, "nobody is signed in" is the normal
   answer, not a failed authentication.
2. `GET /api/v1/analytics/forecast` answered **422** for an account with fewer than two days of balance history.
   The page already showed "Not enough account history to forecast yet", but the 422 still reached the console.

## Decision
- **Refresh:** when the request carries **no refresh cookie**, answer **204 No Content** with no token and no
  `Set-Cookie`. Everything else is unchanged: the CSRF guard (`X-Requested-With` and the origin check) still runs
  first (403); a cookie that is sent but is invalid, expired, revoked or reused is still **401**, and reuse still
  revokes the whole family. The web client treats 204 as signed out.
- **Forecast:** `BalanceForecastResponse` gets an additive `status` field, `"OK"` (default) or
  `"INSUFFICIENT_HISTORY"`. With too little history the endpoint answers **200** with `status:
  "INSUFFICIENT_HISTORY"`, `method: "none"`, empty `points`, `history` and `obligations`, and `end_of_month: null`.
  `end_of_month` is therefore nullable. No figures are invented. Invalid input (horizon outside 7-90) is still 422,
  and a non-owned account is still 404. The assistant's forecast tool answers with the same "at least two days of
  account history" text as before.

## Alternatives considered
- **A non-HttpOnly "session exists" hint cookie** so the app skips the refresh call. It is a second cookie to keep
  in step with the real one (login, rotation, logout, expiry, revocation), and it exposes session state to scripts.
  A 204 is one line and leaks nothing: the caller already knows whether it sent a cookie.
- **Handle both in the web only.** Impossible: the console entry comes from the browser's network layer.
- **Forecast returns 204.** Valid HTTP, but it says less than an explicit status, and the typed client would have
  to pretend an empty body is a `BalanceForecastResponse`.
- **Reword the TASK-089 criterion to "no unexpected console errors".** This keeps two misleading 4xx responses in
  the contract and in the logs and metrics (every anonymous visit counted as an auth failure).

## Consequences
- v1 contract: refresh documents 204 and 401; the forecast gains `status`, and `end_of_month` becomes nullable.
  This tightens one response and adds a state, so a client that read `end_of_month` without checking must now check
  `status` first. The only client is the web app, which is updated in the same PR (`isProjected` in
  `forecastChart.ts`). `docs/api/openapi.json` and `schema.d.ts` are regenerated.
- `scripts/verify_tls.sh` now expects 204 from a cookie-less refresh over HTTPS.
- Anonymous page loads no longer count as 401s in access logs and metrics, so a rise in refresh 401s now means real
  invalid, revoked or reused cookies.
- Threat model: no new attack surface. The 204 says only that no cookie arrived, which the caller already knows.
  Rate limits, CSRF and reuse detection are unchanged.

Standing approval holds: (1) authentication is not weakened: no token is issued without a valid cookie, and CSRF,
rotation and reuse detection are untouched; (2) no money movement; (3) no migration; (4) no new service or
dependency; (5) no invented data: the insufficient state carries no figures; (6) within TASK-089 and TASK-042.
