# PBIP web UI guidelines

The working rules for every web screen. Decision record: [ADR-0016](../adr/0016-web-ui-architecture-and-design-system.md).
Code: tokens in `apps/web/src/styles/tokens.css`, primitives in `apps/web/src/ui/`, helpers in
`apps/web/src/lib/`.

## Visual direction
Quiet, trustworthy, dense enough for real work.

* Neutral surfaces, one brand colour (navy `#00395d`, light blue in dark mode), semantic colours
  only for meaning (success, warning, danger, info). No gradients, no decorative illustrations.
* Flat surfaces with 1px borders. Shadow only on overlays (dialogs, menus). Radius 4-8px.
* System font stack, base 16px, page title 24px (`--text-2xl`), section 20px. Nothing larger.
* Amounts: right-aligned, tabular figures, always through `formatMinor` / `<Amount>`.
* Cards only when they group something; a page is a header plus sections, not a grid of tiles.
* Charts only when a pattern is easier to see than in a table; every chart has a one-line summary
  and a table fallback. Colours `--chart-1..5` in order; never encode meaning by colour alone.

## Tokens
Use `var(--token)`; never write a hex value in a component stylesheet.

| Group | Tokens |
|---|---|
| Surfaces | `--bg`, `--surface`, `--surface-sunken`, `--border`, `--border-strong` |
| Text | `--text`, `--muted` |
| Brand | `--brand`, `--brand-hover`, `--brand-contrast` |
| Semantic | `--ok`, `--warn`, `--danger`, `--info` |
| Charts | `--chart-1` ... `--chart-5` |
| Type | `--text-xs` ... `--text-2xl`, `--weight-medium`, `--weight-strong` |
| Space | `--space-1` (4px) ... `--space-7` (48px) |
| Shape, motion | `--radius-sm`, `--radius`, `--radius-lg`, `--shadow-overlay`, `--duration`, `--control-height` |

Each colour is defined once with `light-dark()`. A new colour needs a contrast check in both
themes: text >= 4.5:1 on `--bg`, `--surface` and `--surface-sunken`; borders and chart marks >= 3:1.

## Components (use these, do not re-create them)
| Need | Use |
|---|---|
| Any button | `<Button variant="primary\|secondary\|ghost\|danger\|link" size loading>`; defaults to `type="button"` |
| Form control | `<Field label hint error>{(control) => <input {...control} />}</Field>` (links label, hint, error) |
| Money entry | `<MoneyInput>` inside a Field; convert with `parseToMinor` before sending |
| Money display | `<Amount minor currency credit>` (credits get a "+" so colour is not the only cue) |
| Status | `<StatusBadge status={transferStatus(s)}>` from `lib/status.ts` (labels from the OpenAPI enums) |
| Loading / empty / error | `<LoadingState>`, `<EmptyState>`, `<ErrorState onRetry>` |
| Result message | `<Alert tone="success\|warning\|info\|error">` (error is `role=alert`, others `role=status`) |
| Page title | `<PageHeader title description actions>` once per page |
| Review before acting | `<ConfirmDialog items confirmLabel busy error>` |
| Other overlays | `<Dialog>` (native `<dialog>`: focus trap, Esc, focus return) |
| Icons | `<Icon name>` (decorative; the control carries the accessible name) |
| Dates | `formatDate`, `formatDateTime` from `lib/format.ts` |
| Error text | `errorMessage(error, fallback)` from `lib/errors.ts`; never show `error.message` directly |

## Financial actions
Every money-moving or destructive action:

1. **Enter**: validate on the client for fast feedback; the server stays the authority.
2. **Review**: `ConfirmDialog` lists from, to, amount and reference exactly as they will be sent.
3. **Submit**: the confirm button shows `loading`; one `Idempotency-Key` per user intent, reused on
   retry and replaced only after a final outcome.
4. **Outcome**: handle every status the endpoint documents (for transfers: `POSTED`,
   `PENDING_STEP_UP`, `PENDING_REVIEW`, `DECLINED`, `REVERSED`) and every documented error. Never
   invent a state the API does not return; never show risk scores or fraud reasons to customers.

## Navigation and roles
Implemented by the routing PR (ADR-0016 decision 5). One shell; customer routes under `/`, staff
routes under `/staff`, chosen from `GET /auth/me` roles. A page appears in navigation only when it
exists and the user's role can use it. Route guards are a convenience; the API enforces access.

## Accessibility (WCAG 2.2 AA)
* One `h1` per page (PageHeader); landmarks `header`, `nav`, `main`.
* Every control has a visible label; errors are linked with `aria-describedby` (Field does this).
* Visible focus (`:focus-visible` outline); keyboard-only use must work; no positive `tabindex`.
* Results are announced (`role=status` / `role=alert`); do not rely on colour alone.
* Touch targets at least 24px, primary actions `--control-height` (40px).
* Respect `prefers-reduced-motion` (`--duration` becomes 0).

## Responsive
Design for 320px first. Tables scroll horizontally inside `.entries`; forms are one column below
640px; nothing causes horizontal page scroll.

## Tests every screen ships with
Loading, empty, error and success states (Vitest + Testing Library, queries by role and label);
keyboard path for the main action; a Playwright journey for critical flows (TASK-077).
