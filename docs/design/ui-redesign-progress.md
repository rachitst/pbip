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
| 2 Landing page, sign in, register, MFA | in review | #524 |
| 3 Customer pages | - | - |
| 4 Staff pages | in review | #523 |
| 4b Backend display text | in review | #519 |
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

## Tests changed

| Test | Change | Reason |
|---|---|---|
| `src/App.test.tsx` | "follows the OS preference..." becomes "starts light even when the OS prefers dark...", with a stubbed dark OS | the owner asked for light by default; the test proves it (it fails on the old code) |
| `src/assistant/AssistantDrawer.test.tsx`, `e2e/journey-assistant.spec.ts` | "Ask the Finora assistant", "Finora assistant", "Looking up how Finora works..." | old "PBIP" display text |
| `src/lib/errors.test.ts` | "We could not reach Finora." | old "PBIP" display text |
| `src/pages/MfaEnrolment.test.tsx` | "Finora will ask for a code" | old "PBIP" display text |
| `src/shell/routing.test.tsx`, `e2e/shell.spec.ts` | page titles "... - Finora" | old "PBIP" display text |

## Backend files whose display text changed

None yet.

## Skipped, and "PBIP" left visible

None yet.
