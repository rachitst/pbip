# ADR 0016: One web UI architecture and design system for customer and staff screens
- Status: Accepted
- Date: 2026-10-30
- Deciders: Architecture Team (product owner approved the proposal on 2026-10-30)

## Context
Only TASK-019 had shipped web code: four customer pages inside a 640px card, navigation held in
React state (no URLs), a theme toggle and one stylesheet. About twenty more screens are planned
(TASK-020/021/022/036/037/048/049/050/051/061/062/065/066/074/081/098-101) by several people. Without a
shared foundation each would invent its own layout, buttons, error text and staff gating. The
inspection found concrete problems:

* **No URLs.** Back leaves the app, reload resets to Accounts, nothing can link to a page. TASK-022
  (open the transaction behind a notification) and TASK-036 (alert detail) need real routes.
* **No role awareness.** The app never calls `GET /auth/me`, so a staff user lands in the customer
  shell and its customer-only calls fail; there is nowhere for staff screens.
* **A 640px card** is too narrow for the audit log (TASK-048), charts (TASK-049) and ledgers.
* **Inconsistent styling and errors:** every `<button>` brand-filled by a global rule with ad hoc
  overrides; dark colours written out three times; an undefined Tailwind class; emoji theme icons;
  date formatters duplicated; some errors showed raw browser text; a problem+json 403 on
  `/transfers` was treated as a transfer result.

## Decision
1. **Design tokens, plain CSS.** Colour, type, spacing, radius, shadow and motion are CSS custom
   properties in `apps/web/src/styles/tokens.css`. Each colour is defined once with `light-dark()`,
   driven by `color-scheme` (OS preference until the user picks a theme). No CSS framework or UI
   kit. Contrast is checked for every token pair (text >= 4.5:1, borders and charts >= 3:1).
2. **Shared primitives in `apps/web/src/ui/`** (Button, Field, MoneyInput, Amount, StatusBadge,
   Alert, Loading/Empty/Error states, PageHeader, Dialog, ConfirmDialog, Icon) and shared helpers in
   `apps/web/src/lib/` (`format.ts`, `errors.ts`, `status.ts`, existing `money.ts`). Feature screens
   compose these instead of writing their own.
3. **Errors from the real contract.** `lib/errors.ts` maps RFC 7807 bodies (`core/errors.py`):
   server-written domain titles are shown; validation detail, 5xx titles and browser network errors
   are replaced by fixed sentences, with the request id for support. Status labels come from the
   OpenAPI enums (`TransferOut.status`), so a new status fails the type check.
4. **Financial actions** follow one pattern: enter -> review (ConfirmDialog) -> submit (busy, one
   Idempotency-Key per intent) -> outcome for every status the API can return. Each task applies it
   to its own flow; the send-money review step belongs to TASK-037 and waits for its dependencies.
5. **Routing and shell (second PR):** `react-router` (data router) gives URLs, deep links and
   lazy-loaded route modules. One responsive shell (sidebar >= 1024px, menu drawer below) serves two
   route trees chosen from `GET /auth/me` roles: customer under `/`, staff under `/staff` (staff role
   and an MFA-verified session). **UI route guards are not a security boundary**: every staff API
   enforces `require_roles(..., mfa=True)` server-side; the guard only avoids showing screens that
   would fail. Hidden pages are not shown in navigation; there are no placeholder pages.
6. **Charts:** Recharts, added by TASK-049 (the first screen that draws one) rather than ahead of
   use, using the `--chart-1..5` palette, each chart with a text summary and a table fallback.
7. **Accessibility target WCAG 2.2 AA** and responsive down to 320px; see `docs/design/ui-guidelines.md`.

## Dependencies (checked 2026-10-30 against React 19.3, Vite 8.3, TypeScript 5.9, Node 22)
* `react-router` 8.4.0 (2026-09-15; peer `react >= 19.2.7`, engine Node >= 22.22, CI uses Node 22
  latest). MIT. Chosen over TanStack Router (we already use TanStack Query, but react-router is the
  more widely known API for a team handover) and over hand-rolled `history` code (focus, scroll and
  lazy loading would be re-implemented).
* `recharts` 3.10.1 (2026-07-25; peer `react ^19`). MIT. Verified in a throwaway install: type check,
  render under jsdom, no `<style>` injection (our CSP has `style-src 'self'`). In 3.10 the `Cell`
  component is deprecated (removed in 4.0) and the strict lint config rejects it; per the package's
  own note, per-slice colours use the chart's `shape` or `content` prop instead. Load it only in the
  lazy Insights route.
* Rejected: a CSS framework (Tailwind) or component kit (MUI, Chakra): heavier, a second styling
  system next to ours, and the product owner asked for neither.

## Consequences
* New screens add a route and compose primitives; they do not add layouts or global CSS.
* TASK-019's work is extended, not replaced: existing pages keep their behaviour, text and tests.
* Two PRs, each independently mergeable: (1) tokens, primitives, formatting, error handling;
  (2) router, role-aware shell, staff route tree, accessibility. TASK-049 needs only (1) and the
  customer routes of (2); it does not wait for any staff page. TASK-048 needs the staff tree of (2).
* `light-dark()` is native from Chrome 123 / Firefox 120 / Safari 17.5. The production build does not
  depend on that: Vite's Lightning CSS compiles it to custom-property toggles keyed on the same
  `color-scheme` / `data-theme` rules (checked in the built CSS), so older browsers get both themes.

## Implementation (2026-10-30)
* PR #200: tokens, primitives, formatting, error mapping.
* PR #202: `react-router` 8.4.0 data router (`createBrowserRouter`), created once per signed-in
  session so no route state outlives a sign-out. `SessionRoot` loads `GET /auth/me`; `CustomerArea`
  and `StaffArea` choose the layout from its roles (staff-only users are redirected from customer
  URLs to `/staff`; customers see "not found" for staff URLs). Staff pages show a warning when the
  session is not MFA-verified, because every staff API will refuse it. Verified against the backend:
  roles from `RoleName`/`STAFF_ROLES` (`auth/models.py`), `mfa_verified_session` from the token's
  `amr` (`otp`), and refresh keeps `amr`, so a reloaded staff session stays verified.
* Accessibility in the shell: skip link, one `main` landmark, focus moves to the page `h1` after
  navigation, `document.title` per page, `aria-current` on navigation links, Menu button with
  `aria-expanded`/`aria-controls`, closing on navigation and Esc.
* Not built here, because no task owns them: registration and MFA-enrolment screens (the APIs
  exist; TASK-077's first journey assumes them; tracked in #201), and a staff user-administration screen.
