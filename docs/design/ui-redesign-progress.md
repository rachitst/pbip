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
| 5 Polish, accessibility, `make check`, docs, v1.2.0, deploy | done | #527, #528, this PR |
| Re-record walkthrough, demo cut, slide screenshots | done | this PR (tooling), files outside the repo |

## Final summary (21 Nov 2026)

**Merged:** #518 (audit, design system, ADR-0063, before screenshots), #520 (CI repair: backup drill and two
CVEs), #521 (foundation), #523 (staff pages), #519 (backend display text), #525 (customer pages), #524 (landing,
sign-in, MFA, profile, assistant), #527 (polish, after screenshots, docs, demo tooling), #528 (smoke test, label in
name), and this PR (card layout fix, frame-exact editor, user-manual screenshots, this summary).

**Checks:** `make check` on the tree of v1.2.0: 2,919 passed, 12 skipped, coverage 96.43% (target: at least the
96.4% of before), web 470 tests in 52 files, all static checks green; every PR's CI Gate green and up to date before
its squash merge.

**Releases and deploys:**
- `v1.2.0`: the tag deploy installed the release, then its smoke test failed on one check (it looked for "PBIP" in
  the page) and the pipeline rolled back to v1.1.4 as designed (run 44898486154).
- `v1.2.1`: smoke test and label-in-name fixes; tag deploy succeeded, smoke test passed (run 44903592445).
- `v1.2.2`: this PR's fixes; deployed the same way (run listed on the release).
- **Live check** (https://pbip.duckdns.org): title "Finora - Smart personal banking", the Finora favicon, the landing
  page with the embedded sign-in form, readiness OK, and `scripts/smoke_test.py` passes against it.

**Recordings** (outside the repo, in `~/Desktop/Rachit/finora-video/`; the videos are attached to the v1.2.0
release):
- `finora-walkthrough.mp4` (11:31, H.264), `.srt`, `.chapters.txt`: the A-to-Z walkthrough re-recorded on a fresh
  seed with the existing recorder, same story, steps and pages; captions changed only where they said PBIP. Take 2
  (on the v1.2.1 UI) ran 59 PASS and 1 DIFF: its assistant answered with the rule-based fallback because Groq did
  not answer, so the assistant chapter is take 1's (same UI, model answers), merged with `merge-takes.py`; every
  step of the merged film is PASS. `finora-walkthrough-highlights.mp4` (3:02) too. MP4 only (owner's decision).
- `finora-demo-cut.mp4` and `finora-demo-cut.json`: the owner's 15-clip cut, 4:40.5, 1920x1080, H.264, no audio,
  0.25 s fades, built by `demo/record/build-demo-cut.py`; each clip found by its captions, short clips hold their
  last frame, long ones are trimmed; the Grafana clip shows the API panel only.
- `slides/` (1920x1080, demo data, fresh seed): `accounts.png`, `transfer-review.png`, `fraud-alert.png`,
  `assistant.png`, `insights.png`, `audit-log.png`.

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

## Phase 5 (21 Nov 2026): polish, accessibility, docs

- **After screenshots:** `docs/design/screenshots/after/`, the same 32 screens x 2 widths and file names as the
  before set, taken on a freshly seeded stack built from `main` with the fraud lab's F1, F2 and F5 run first.
  The landing page's product screenshots were re-taken on the final UI ("landing assets").
- **axe-core 4.14 (WCAG 2.0/2.1 A and AA rules), 31 views x light and dark = 62 scans** on the local stack (axe
  installed in a scratch folder, not a project dependency): **no colour-contrast violations** in either theme;
  30 scans clean. One rule, *label in name* (2.5.3), was reported on four controls:
  - security-monitoring "Review"/"View" links (named "Open the alert for customer ..."): **fixed**, the name now
    starts with the visible word (the test's name match still holds);
  - **notification bell:** the only visible text is the unread count, which the name contains ("Notifications
    (2 unread)"); axe's heuristic still flags it. Kept: the name and the badge text are asserted by tests;
  - **"Ask Finora" button** (was named "Ask the Finora assistant") and the fraud lab's **"Run scenario"** buttons
    (named "Run F1 APP scam" ...): **fixed after the owner approved it (21 Nov)**: the button's name is now
    "Ask Finora", its visible label; the lab buttons read "Run F1" ... "Run F6", the start of their names.
    The tests and recorder selectors changed only for the new names (see "Tests changed").
- **Keyboard and focus:** every control keeps a visible 2 px teal focus ring; the skip link, the Menu button, Esc on
  the menu, drawer and dialogs, and focus-to-heading after navigation are unchanged (E2E `shell.spec.ts`).
- **360 px:** every page in the after set at 360 px has no horizontal page scroll; wide tables scroll inside their
  card.
- **Docs:** README product name and latest release, the user manual (customer, staff and compliance guides) renamed
  to Finora; CHANGELOG `[1.2.0]` and `docs/release/v1.2.0.md`. The user-manual screenshots are replaced from the
  walkthrough re-recording after v1.2.0.

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

| `src/assistant/AssistantDrawer.test.tsx`, `e2e/journey-assistant.spec.ts` | the button name "Ask the Finora assistant" becomes "Ask Finora" | WCAG 2.5.3 label in name, owner-approved (#528) |
| `src/pages/staff/FraudDemoLab.test.tsx` | `getAllByText("Run scenario")` becomes `getAllByText(/^Run F\d$/)` (still 5 buttons) | the buttons' visible text, WCAG 2.5.3, owner-approved (#528) |
| `demo/record/record-walkthrough.ts`, `record-demo.ts`, `capture-slides.ts`, `capture-screens.ts` | assistant button selector `{ name: "Ask Finora", exact: true }` | recorder selectors for the new name (#528) |
| `scripts/smoke_test.py` | the served page must contain "Finora" (was "PBIP") | old display text; it failed the v1.2.0 deploy's smoke test, which rolled back to v1.1.4 (#528) |

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
- **Authenticator apps enrolled before v1.2** keep their "PBIP" account label (stored in the app); new enrolments
  show "Finora".
- **Skipped:** the VP9 `.webm` versions of the videos (owner's decision); no other item of the brief was skipped.
