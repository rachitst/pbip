# UI redesign and rebrand to Finora: progress

Owner's brief (21 Nov 2026): rebrand PBIP to **Finora** ("Smart personal banking", assistant "Ask Finora")
and redesign the web UI, then release v1.2.0, deploy it, re-record the walkthrough and the demo cut, and
capture the slide screenshots, all by the end of 23 Nov (IST); the presentation is on 25 Nov. The design
system is in [ui-design-system.md](ui-design-system.md).

## Rules this work follows

- **Web (`apps/web`):** markup, styling, static content and display text only. Hooks, API clients,
  validation, the auth/MFA flow, state handling and routes are not changed.
- **Outside `apps/web`:** only string literals of user-facing display text that contain the old name.
  Every file touched is listed below.
- **Tests:** accessible names, roles, labels and test ids stay. A test changes only where it asserts
  styling, markup structure or the old "PBIP" text, and every change is listed below with its reason. No
  assertion is deleted or weakened.
- **Accessibility:** WCAG 2.1 AA contrast (checked for every token pair in both themes), visible focus,
  keyboard use, layouts that work at 360 px.
- **CSP:** fonts, icons and images are bundled; nothing is loaded from a CDN.
- **Git:** branches and PRs; `CI Gate` and the PR-title check green and up to date before a squash merge;
  no draft or WIP merges; no AI co-author trailers. The `main` ruleset needs no human approval
  (required approving reviews: 0), so no PR waits on a reviewer.

## Status

| Phase | State | PR |
|---|---|---|
| 0 Audit, design system, before screenshots | done | #518 |
| 1 Foundation: tokens, font, brand, components, shell | done | #521 |
| 2 Landing page, sign in, register, MFA | done | #524 |
| 3 Customer pages | done | #525 |
| 4 Staff pages | done | #523 |
| 4b Backend display text | done | #519 |
| 5 Polish, accessibility, `make check`, docs, v1.2.0, deploy | - | - |
| Re-record walkthrough, demo cut, slide screenshots | - | - |

## Phase 0 (21 Nov 2026)

- **Audit and design system:** [ui-design-system.md](ui-design-system.md): the current app, the brand,
  tokens (colour with contrast checked, type, spacing, radius, shadow), the component list, every page
  mapped to a phase, and every "PBIP" in the web app and in user-facing backend text.
- **Before screenshots:** `docs/design/screenshots/before/`, 32 screens x 2 widths (desktop 1440 px,
  phone 360 px), file names `<area>-<page>--<desktop|phone>.png`, captured by
  `demo/record/capture-screens.ts` (`capture.config.ts`). Long pages are cut at 3,200 px.
- **Demo data for the screenshots:** the local stack's database was backed up
  (`~/.local/share/pbip-lead/backups/local-pbip-20261121T065452Z.dump`, outside the repo), recreated
  empty, migrated and seeded with `scripts/seed_dev.py`. `SCREENS_PREPARE=1` then runs the fraud demo
  lab's F1, F2 and F5 as the analyst so the alert queue, alert detail and dashboards have content. The
  after set is taken the same way, on a fresh seed, so the two sets match.
- **Decisions:**
  - The landing page appears only at `/` for a signed-out visitor; any other signed-out URL still shows
    sign-in in place so deep links survive, and signed-in users land where they land today.
  - Light theme by default: a first visit is light whatever the OS setting; the toggle still switches
    to dark and the choice is remembered as before.
  - One small dependency each for the font and the icons (Phase 1 records the justification).

## Phase 1 (21 Nov 2026)

- **Tokens:** `apps/web/src/styles/tokens.css` replaced with the Finora palette and scales from the design
  system (contrast checked for every pair, both themes).
- **Dependencies** (justification, rule 5):
  - `@fontsource-variable/inter` (OFL-1.1): Inter as bundled woff2 files served by our own nginx, about
    25 kB per subset and only the subsets a page needs are downloaded; no CDN, no CSP change. The
    alternative, the system font, does not give a consistent look across Windows, macOS and Android.
  - `lucide-react` (ISC): one consistent icon set, tree-shaken so only the imported icons are bundled
    (each a small inline SVG component). The alternative, drawing every icon by hand, costs time and
    consistency. No UI component library was added.
- **Brand:** original logo mark and wordmark (`src/brand/Logo.tsx`, `public/favicon.svg`), title and meta
  tags, every user-visible "PBIP" in the web app renamed to Finora.
- **Shared components and shell:** buttons, form controls, cards, badges, alerts, tables, states, dialog,
  `.stat-tile`, `.segmented`; a sidebar with logo and icon navigation and a sticky top bar for both areas.
- **CSS order:** `main.tsx` imports the global styles before `App`, so page stylesheets (imported by
  their pages) come later in the bundle and win over shared rules of the same specificity.
- **Light by default:** a first visit is light whatever the OS prefers; the toggle and its saved choice
  work as before.
- **Preview against the local stack:** each branch is built and served with `vite preview` over HTTPS,
  proxying `/api` to the stack (a git-excluded local config); the proxy presents the stack's own Origin
  because the API's CSRF guard allows only that origin on cookie endpoints.
- **CI repair found on the way (#520):** `main` had turned red without a code change: the runner image's
  newer `pg_dump` wrote a setting PostgreSQL 16 rejects (the backup drill now runs the tools inside the
  database service container), and two new CVEs (dnspython 2.9.2, tornado 6.6.1).
- **Branches are updated by merging `main`,** never rebased and force-pushed (owner's rule).

## Phases 2-4 and 4b (21 Nov 2026), built in parallel

Three page agents worked in their own worktrees from the foundation, each previewing its branch against the
local stack; the lead reviewed every diff for logic changes (none: markup, classes, icons and wrappers
only) and merged in turn, updating the other branches by merging `main`.

- **Phase 2, #524 (public and sign-in):** a landing page at `/` for signed-out visitors only
  (`pages/Landing.tsx`): hero with the value proposition, a "Sign in" link to the form, "Open an account",
  and the real sign-in form embedded (the E2E helpers and the recorder open `/` and sign in there, so the
  form's labels and the one "Sign in" button stay); feature sections with real product screenshots
  (`src/assets/landing/*.webp`, captured by the "landing assets" test in `demo/record/capture-screens.ts`);
  "How we keep you safe"; figures only from the reports (2,345 tests in CI on 3 Nov, test report section
  2.2; recall 0.923, PR-AUC 0.945 and p95 20.2 ms for the served model on synthetic data, model report
  section 10); a footer linking the repository and docs; a plain "simulated bank, capstone project, no
  real money" note. Sign-in, the code step and registration share a presentational `AuthFrame`
  (an `embedded` prop only changes the wrapper and heading level). MFA enrolment, Profile & security,
  Not found, the Ask Finora drawer and the notification tray restyled. New tests:
  `src/pages/Landing.test.tsx` (4).
- **Phase 3, #525 (customer pages):** total-balance hero card with stat tiles, account cards, transaction
  filters and tables in cards, send-money outcome cards (posted, held, declined, update) with the same
  roles and messages, payees, bills, scheduled transfers, statements, insights and forecast, loans,
  wealth, invest, SIP, insurance, compare and recommendations; phone layouts reworked so balances stack.
  New presentational components `OutcomeCard.tsx` and `EmptyCard.tsx`.
- **Phase 4, #523 (staff pages):** full-width console layout, stat tiles with definitions, tables in
  cards, a segmented alert filter, reason badges coloured by source next to their text label (display-only
  `SOURCE_STYLE`), and the lab, monitoring, compliance, audit log, RM insights, behavioural analytics and
  administration panels. Table-cell row buttons moved into a wrapper so the cells stay table cells.
- **Phase 4b, #519 (backend display text):** only string literals that show the old name to users.
- **Per-page CSS:** page sections moved out of `styles.css` into files next to their pages
  (`Auth.css`, `Landing.css`, `MfaEnrolment.css`, `Profile.css`, `NotFound.css`,
  `assistant/AssistantDrawer.css`, `CustomerPage.css`, `Accounts.css`, `Insights.css`, `Loans.css`,
  `Wealth.css`, `Statements.css`, `ProductRecommendations.css`, `staff/staff.css`); shared rules the
  staff pages reuse (`.meter`, `.insights-chart`, `.insights-legend`, `.choice-group`) stay global, and
  the rules the CSS-reading tests name stay where the tests read them.
- **Selectors the recorders and E2E rely on** were kept, including the secret blur targets
  (`code.secret`, `ul.recovery-codes`, `a[href^="otpauth:"]`) so MFA keys never reach a recording.

## Tests changed

| Test | Change | Reason |
|---|---|---|
| `src/App.test.tsx` | "follows the OS preference..." becomes "starts light even when the OS prefers dark...", with a stubbed dark OS | the owner asked for light by default; the test proves it (it fails on the old code) |
| `src/assistant/AssistantDrawer.test.tsx`, `e2e/journey-assistant.spec.ts` | "Ask the Finora assistant", "Finora assistant", "Looking up how Finora works..." | old "PBIP" display text |
| `src/lib/errors.test.ts` | "We could not reach Finora." | old "PBIP" display text |
| `src/pages/MfaEnrolment.test.tsx` | "Finora will ask for a code" | old "PBIP" display text |
| `src/shell/routing.test.tsx`, `e2e/shell.spec.ts` | page titles "... - Finora" | old "PBIP" display text |
| `apps/api/tests/integration/test_statements.py` | `b"PBIP Bank"` becomes `b"Finora"` | the PDF header's old display text (#519) |
| `apps/api/tests/integration/test_assistant.py`, `apps/api/tests/unit/test_assistant_discovery.py` | the wealth footer says "simulated Finora rates", "simulated by Finora" | old display text (#519) |
| `apps/api/tests/integration/test_assistant_adversarial.py` | FAQ snippets "Insurance in Finora", "...bonds in Finora are simulated" | old display text (#519) |

Added (no existing test changed): `src/pages/Landing.test.tsx` (4 tests, #524). Phases 3 and 4 changed no
test.

## Backend files whose display text changed (#519, string literals only)

| File | Text |
|---|---|
| `apps/api/src/pbip_api/main.py` | OpenAPI title "Finora API", description "Finora - smart personal banking - backend API" |
| `apps/api/src/pbip_api/modules/statements/service.py` | PDF header "Finora" (was "PBIP Bank"), PDF title "Finora statement ..." |
| `apps/api/src/pbip_api/modules/statements/pdf.py` | PDF `/Producer (Finora)` |
| `apps/api/src/pbip_api/modules/auth/service.py` | TOTP `issuer_name="Finora"` (the label in an authenticator app; verification does not use it) |
| `apps/api/src/pbip_api/modules/assistant/prompts.py` | routing prompts: "routing step of Finora's assistant" |
| `apps/api/src/pbip_api/modules/assistant/guard.py` | refusal text: "Finora's products ... how Finora works" |
| `apps/api/src/pbip_api/modules/assistant/service.py` | capability text: "funds Finora offers" |
| `apps/api/src/pbip_api/modules/assistant/tools.py` | `get_faq_answer` tool description "How Finora works: ..." |
| `apps/api/src/pbip_api/modules/assistant/discovery.py` | wealth footer sentences (the f-string was wrapped in parentheses for the line length; same value) |
| `apps/api/src/pbip_api/modules/assistant/faq.py` | eight FAQ answers |
| `apps/api/src/pbip_api/modules/loans/schemas.py` | field description "active Finora EMIs" |
| `apps/api/src/pbip_api/modules/wealth/schemas.py` | three field descriptions, `PRICE_SOURCE_DESCRIPTION`, and the `IndicativePremiumOut` docstring (published as its OpenAPI description) |
| `docs/api/openapi.json`, `apps/web/src/api/schema.d.ts` | regenerated with `make openapi` (descriptions only) |

## Skipped, and "PBIP" left visible

- **Identifiers and stored values (rule 2), kept:** the bank code `PBIP0001` (shown as a payee's bank
  code), the demo e-mails `@demo.pbip`, the fraud lab's stored payee name "PBIP Lab Merchant", the
  statement download name `pbip-statement-<start>-to-<end>.pdf` (a filename from the API's
  Content-Disposition, not on the approved list), storage keys, the problem `type` URIs, the domain
  `pbip.duckdns.org`, package, service and workflow names.
- **Grafana dashboards** keep their PBIP titles: an operators' tool outside the product UI, provisioned by
  file and uid.
- **The repository README** names the product Finora and says the code keeps the project name PBIP.
