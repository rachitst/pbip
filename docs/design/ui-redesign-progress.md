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
| 0 Audit, design system, before screenshots | done | this PR |
| 1 Foundation: tokens, font, brand, components, shell | - | - |
| 2 Landing page, sign in, register, MFA | - | - |
| 3 Customer pages | - | - |
| 4 Staff pages | - | - |
| 4b Backend display text | - | - |
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

## Tests changed

None yet.

## Backend files whose display text changed

None yet.

## Skipped, and "PBIP" left visible

None yet.
