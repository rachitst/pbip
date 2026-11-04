# Accepted security risks

A risk is accepted here only by the product owner, with the reason, the exposure and a trigger to look at it again.
An accepted risk is not a closed one: each entry says what will reopen it.

## RA-1: `minimatch` ReDoS in the OpenAPI type generator (dev tooling only), #232

| Field | Value |
|---|---|
| Accepted by | Product owner (Rachit Chheda), 4 November 2026, on #232 |
| Advisory | [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249), `minimatch` < 11.0.1, moderate: crafted glob patterns can make matching take very long (denial of service) |
| Where it is | `apps/web` dev dependency `openapi-typescript` 7.13.0 → `@redocly/openapi-core` 1.34.x → `minimatch` 5.1.9. `npm audit` lists the one chain as 3 moderate entries |
| Exposure | **None in production.** The tool runs only when a developer or CI regenerates `apps/web/src/api/schema.d.ts` from the repository's own `docs/api/openapi.json` (`make openapi`): trusted input, no network-facing use. Production ships only the static web build behind nginx; there is no Node runtime (AGENTS.md section 1). `npm audit --omit=dev` reports 0 vulnerabilities |
| Why it is not fixed | No fixed release exists. The newest `openapi-typescript` (7.13.0) still requires `@redocly/openapi-core` ^1.34, which `require`s `minimatch` 5 and calls it as a function. Forcing `minimatch` 11 with an npm override (as #434 did for ESLint) would break that call, because version 11 no longer exports a callable module. `npm audit fix --force` would downgrade `openapi-typescript` to 6.x, a breaking change to the generated types |
| What was done | #434 removed 10 of the original 13 entries (ESLint and typescript-eslint now get `minimatch` 11). This chain is the remainder |
| Detection | Dependabot alerts and Dependabot security updates are enabled for the repository, and `.github/dependabot.yml` checks `/apps/web` npm weekly, so a fixed release is proposed as a PR. CI's `npm audit --audit-level=high` keeps failing the build on any high or critical advisory |
| Re-check when | `openapi-typescript` or `@redocly/openapi-core` releases a version that depends on `minimatch` >= 11.0.1 (Dependabot PR or alert), or the advisory is raised to high, or the generator starts reading untrusted input. Then: upgrade, run `make openapi`, confirm `schema.d.ts` is unchanged or reviewed, and close this entry |
