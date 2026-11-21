# Finora design system (UI redesign, November 2026)

The web app is renamed **Finora** ("Smart personal banking"; the assistant is **Ask Finora**) and restyled.
The repository, the domain `pbip.duckdns.org`, package names, code identifiers, environment variables,
Docker/compose service names and CI workflow names keep the PBIP name. Progress, decisions and the test
changes are in [ui-redesign-progress.md](ui-redesign-progress.md).

## 1. Where the app stands today (audit, 21 Nov 2026)

- **Stack:** React 19, React Router 8, TanStack Query, Recharts (lazy-loaded), Vite. No UI library.
- **Styling:** one global stylesheet, `apps/web/src/styles.css` (1,790 lines), on top of
  `apps/web/src/styles/tokens.css` (ADR-0016: every colour defined once with `light-dark()`). Some pages
  have their own CSS file (`ChoiceGroup.css`, `LoanCard.css`, `WealthCompare.css`, `PhoneSection.css`,
  `staff/dashboards.css`, `staff/Administration.css`, `staff/RMInsights.css`,
  `staff/RmAssignmentControl.css`, `ui/Dialog.css`).
- **Shared components (`apps/web/src/ui/`):** `Button`, `Field`, `MoneyInput`, `Alert`, `Amount`,
  `StatusBadge`, `Dialog`, `ConfirmDialog`, `PageHeader`, `States` (loading, empty, error) and `Icon`
  (6 hand-drawn paths).
- **Shell (`apps/web/src/shell/`):** `AppShell` (header with a text "PBIP" brand, a left navigation that
  collapses behind a Menu button under 1024 px, the main region), `CustomerSessionBar`, the staff session
  bar in `routes.tsx`, `NotificationBell`, `ThemeToggle`.
- **Signed out:** `App.tsx` shows the sign-in page in place on every URL, so a deep link survives
  sign-in; registration swaps in place. There is no landing page.
- **Look:** system font, navy `#00395d` brand, small radii (4-8 px), flat white cards on a grey page,
  outlined buttons. Functional and accessible, but plain, and the navy is close to a real bank's
  colour, which the new palette avoids.
- **Constraints that shape the redesign:**
  - CSP `default-src 'self'; style-src 'self'; img-src 'self' data:` (`infra/nginx/security-headers.conf`):
    no CDN, no Google Fonts, no inline `<style>`; fonts and images are bundled.
  - `src/pages/LayoutStyles.test.ts` and the notification and audit-log style tests read CSS source:
    the rules they name must stay (they guard phone-width bugs).
  - Unit and E2E tests find elements by role, accessible name, label and text; those names stay.

## 2. Brand

- **Name:** Finora. **Tagline:** Smart personal banking. **Assistant:** Ask Finora.
- **Logo mark:** a rounded square in the primary navy holding a stylised lower-case "f" whose crossbar
  continues as a rising teal line (growth and a ledger line); it reads at 16 px as a favicon.
  Original artwork, drawn for this project (`apps/web/src/brand/`), no resemblance to a real bank's mark.
- **Wordmark:** "Finora" set in Inter Semibold with tight tracking, navy, beside the mark. In dark mode the
  mark keeps its navy square with a light border and the wordmark turns light.
- **Voice:** plain, calm and exact, as today. The product name appears where a person needs it (page
  titles, the sign-in page, the assistant); it is not sprinkled into every sentence.

## 3. Tokens

Defined once in `tokens.css` with `light-dark()`; `data-theme` on `<html>` picks the scheme. The light
theme is the default. Text tokens are at least 4.5:1 on `--bg`, `--surface` and `--surface-sunken`;
borders that identify a control (`--border-strong`) and chart colours at least 3:1 on `--surface`.

### Colour (light / dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#f5f7fa` | `#0b1220` | page background |
| `--surface` | `#ffffff` | `#111a2c` | cards, panels, shell |
| `--surface-sunken` | `#eef2f7` | `#0d1526` | table headers, wells, code |
| `--surface-raised` | `#ffffff` | `#16213a` | menus, drawers, dialogs |
| `--text` | `#0f1d33` | `#e7edf6` | body text, headings |
| `--muted` | `#526079` | `#9eabc0` | secondary text |
| `--border` | `#dfe5ee` | `#25324b` | dividers, card edges (decorative) |
| `--border-strong` | `#7c889b` | `#66768f` | input and control borders (3:1) |
| `--brand` | `#1a3561` | `#8fb2f2` | primary buttons, links, active nav |
| `--brand-hover` | `#122849` | `#b0c9f7` | hover/pressed |
| `--brand-contrast` | `#ffffff` | `#0b1220` | text on `--brand` |
| `--brand-soft` | `#e9eef8` | `#1b2944` | active nav background, selected rows |
| `--accent` | `#0b7a72` | `#3fd0bf` | the one teal accent: highlights, focus ring, positive figures |
| `--accent-soft` | `#e3f4f2` | `#10302e` | accent backgrounds |
| `--ok` | `#147a3e` | `#5fd394` | success |
| `--warn` | `#965a04` | `#f1c25a` | warning |
| `--danger` | `#b42318` | `#ff8f86` | danger |
| `--info` | `#1f5bb8` | `#86b6ff` | information |
| `--ok-soft`, `--warn-soft`, `--danger-soft`, `--info-soft` | tinted backgrounds for alerts and badges | | |
| `--chart-1..5` | `#2357a8`, `#0b7a72`, `#b8730a`, `#6a4fb8`, `#c0436c` | lighter equivalents | categorical charts, in order |
| `--focus` | `= --accent` | | 2 px focus ring with a 2 px offset |

### Type

Inter (variable, self-hosted through `@fontsource-variable/inter`, OFL-1.1), falling back to the system
UI font. Numbers use `font-variant-numeric: tabular-nums` in tables, amounts and stat tiles.

| Token | Size / line height | Use |
|---|---|---|
| `--text-xs` | 0.75 rem / 1.4 | badges, captions |
| `--text-sm` | 0.875 rem / 1.5 | secondary text, tables, labels |
| `--text-base` | 1 rem / 1.55 | body |
| `--text-lg` | 1.125 rem | card titles (h3) |
| `--text-xl` | 1.25 rem | section titles (h2) |
| `--text-2xl` | 1.625 rem | page titles (h1) |
| `--text-3xl` | 2.25 rem | stat values, landing headings |
| `--text-4xl` | 3 rem (clamped down on phones) | landing hero |

Weights: 400 body, 500 labels and nav (`--weight-medium`), 600 headings and buttons (`--weight-strong`),
700 for the hero only. Headings use a slight negative letter spacing (-0.01 em).

### Spacing, radius, shadow, motion

- Spacing: a 4 px base, `--space-1` 4 px, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-5` 24,
  `--space-6` 32, `--space-7` 48, `--space-8` 64 (landing sections).
- Radius: `--radius-sm` 6 px (badges, small controls), `--radius` 10 px (inputs, buttons),
  `--radius-lg` 14 px (cards, dialogs), `--radius-pill` 999 px.
- Shadow: `--shadow-sm` (cards: `0 1px 2px rgb(15 29 51 / 0.06)`), `--shadow` (hover, menus),
  `--shadow-overlay` (dialogs and drawers). Dark mode leans on borders, with lighter shadows.
- Motion: `--duration` 150 ms, ease-out; 0 ms under `prefers-reduced-motion`.
- Layout: content width up to 1200 px; the sidebar 256 px; the top bar 64 px; the control height 2.5 rem
  (44 px touch targets on phones).

## 4. Components

Each is styled through classes in `styles.css` (or a React component in `src/ui/` where one exists). The
list is what Phase 1 delivers; pages adopt them in Phases 2-4.

| Component | Where | Notes |
|---|---|---|
| Button (primary, secondary, ghost, danger, link; sm/md) | `ui/Button.tsx`, `.btn` | filled navy primary; secondary is outlined; loading state kept |
| Input, select, textarea, checkbox, radio | `ui/Field.tsx`, `ui/MoneyInput.tsx`, form rules | 10 px radius, strong border, teal focus ring; radios keep `ChoiceGroup.css` |
| Card | `.card`, `.panel` | white, 14 px radius, hairline border, small shadow |
| Stat tile | `.stat-tile` (new class), dashboards | label, large tabular value, definition line |
| Table | `table`, `.table-wrap` | sunken header, row dividers, hover tint, horizontal scroll region on phones |
| Badge / status | `ui/StatusBadge.tsx`, `.badge` | soft background + text colour + text; never colour alone |
| Alert | `ui/Alert.tsx` | soft tone background, left accent bar, icon |
| Tabs / segmented filter | `.segmented` (new), existing filter button rows | pressed state from `aria-pressed`, no logic change |
| Modal | `ui/Dialog.tsx`, `ui/ConfirmDialog.tsx` | native `<dialog>`, raised surface, overlay |
| Drawer | assistant drawer, notification tray | raised surface, slides from the right, Esc closes (unchanged) |
| Toast | `role="status"` success messages | inline status banners in the page flow (the app has no toast queue; none is added) |
| Empty / loading / error states | `ui/States.tsx` | icon, message, retry; skeleton-free (a spinner + text) |
| Page header | `ui/PageHeader.tsx` | title, description, actions; sets `document.title` to "<page> - Finora" |
| App shell | `shell/AppShell.tsx` | logo + wordmark, icon navigation, top bar with session, theme and sign-out |
| Logo | `brand/Logo.tsx` (new) | mark + wordmark SVG, decorative inside a labelled link/heading |

## 5. Pages and phases

| Phase | Page (route) | Source |
|---|---|---|
| 1 | App shell, customer and staff (sidebar, top bar, page header) | `shell/*`, `ui/*`, `styles.css` |
| 2 | Landing page for signed-out visitors (`/`) | new `pages/Landing.tsx` |
| 2 | Sign in, authenticator code, register | `pages/Login.tsx`, `pages/Register.tsx` |
| 2 | MFA setup (Profile and staff Sign-in security), recovery codes | `pages/MfaEnrolment.tsx` |
| 3 | My accounts, total balance, open an account, account transactions | `Accounts.tsx`, `TotalBalance.tsx`, `AccountTransactions.tsx` |
| 3 | Send money: review, held, step-up, receipt | `SendMoney.tsx` |
| 3 | Payees | `Payees.tsx` |
| 3 | Pay bills | `PayBills.tsx` |
| 3 | Scheduled transfers | `ScheduledTransfers.tsx` |
| 3 | Statements | `Statements.tsx` |
| 3 | Notifications (bell and tray) | `shell/NotificationBell.tsx` |
| 3 | Spending & financial health, balance forecast | `Insights.tsx`, `BalanceForecastSection.tsx` |
| 3 | Loans | `Loans.tsx` |
| 3 | Wealth, invest, SIP, insurance quote, compare | `Wealth.tsx`, `WealthHoldings.tsx`, `Invest.tsx`, `StartSip.tsx`, `InsuranceQuote.tsx`, `WealthCompare.tsx` |
| 3 | Recommendations | `ProductRecommendations.tsx` |
| 3 | Ask Finora assistant drawer | `assistant/AssistantDrawer.tsx` |
| 3 | Profile & security, phone, demographics, risk profile, consent | `Profile.tsx`, `PhoneSection.tsx`, `DemographicsSection.tsx`, `RiskProfileSection.tsx`, `AnalyticsConsentSection.tsx` |
| 3 | Not found | `NotFound.tsx` |
| 4 | Staff overview | `staff/StaffHome.tsx` |
| 4 | Security monitoring | `staff/SecurityMonitoring.tsx` |
| 4 | Fraud alerts, alert detail | `staff/FraudQueue.tsx`, `staff/FraudAlert.tsx` |
| 4 | Fraud dashboard | `staff/FraudDashboard.tsx` |
| 4 | Fraud demo lab | `staff/FraudDemoLab.tsx` |
| 4 | Compliance & risk | `staff/ComplianceDashboard.tsx` |
| 4 | Audit log | `staff/AuditLog.tsx` |
| 4 | Customer insights (RM), customer flyout | `staff/RMInsights.tsx`, `staff/CustomerFlyout.tsx`, `staff/RmAssignmentControl.tsx` |
| 4 | Behavioural analytics | `staff/BehaviourAnalytics.tsx` |
| 4 | Administration (staff users, settings) | `staff/Administration.tsx`, `AdminStaffUsers.tsx`, `AdminSettings.tsx` |
| 4b | Backend display text | see section 7 |

## 6. "PBIP" in the web app

| File | Text | Change |
|---|---|---|
| `index.html` | `<title>PBIP - Personal Banking</title>` | "Finora - Smart personal banking", description meta, favicon |
| `shell/AppShell.tsx` | brand "PBIP" | Finora logo |
| `ui/PageHeader.tsx` | `` `${title} - PBIP` `` | `- Finora` |
| `assistant/AssistantDrawer.tsx` | "Ask PBIP", "Ask the PBIP assistant", "PBIP assistant", "Looking up how PBIP works..." | Ask Finora / Finora assistant |
| `pages/Login.tsx` | "New to PBIP?" | "New to Finora?" |
| `pages/MfaEnrolment.tsx` | "PBIP will ask for a code" | Finora |
| `lib/errors.ts` | "We could not reach PBIP." | Finora |
| `lib/investing.ts`, `lib/recommendations.ts`, `pages/InsuranceQuote.tsx`, `pages/Wealth.tsx`, `pages/WealthCompare.tsx`, `pages/Payees.tsx` (hint) | "PBIP does no underwriting", "simulated by PBIP's price model", "You cannot apply for this in PBIP yet", ... | Finora |
| **Kept:** `PBIP0001` (the bank code, a stored value), `pbip-statement-...` download name and `pbip-theme`, `pbip.device-id`, `pbip-web` (storage keys and a header value), `@demo.pbip` demo e-mails, "PBIP Lab Merchant" (a payee name the lab stores) | | identifiers or stored values (rule 2) |

## 7. "PBIP" in user-facing backend text (Phase 4b)

Only the string literals change; identifiers, stored values and contract values stay.

| File | Text | Decision |
|---|---|---|
| `apps/api/src/pbip_api/main.py` | OpenAPI `title="PBIP API"`, `description="Personal Banking Intelligence Platform - backend API"` | change (OpenAPI title and description) |
| `modules/statements/service.py` | PDF header "PBIP Bank"; PDF title "PBIP statement ..." | change |
| `modules/statements/pdf.py` | PDF `/Producer (PBIP)` | change |
| `modules/auth/service.py` | TOTP `issuer_name="PBIP"` (the label an authenticator app shows) | change; verification does not use the issuer, existing enrolments keep working |
| `modules/assistant/prompts.py` | "routing step of PBIP Bank's assistant" (system prompt) | change |
| `modules/assistant/guard.py` | refusal text "PBIP's products ... how PBIP works" | change |
| `modules/assistant/service.py` | capability text "which loans, deposits and funds PBIP offers" | change |
| `modules/assistant/tools.py` | tool description "How PBIP works: ..." | change (prompt wording) |
| `modules/assistant/discovery.py` | "simulated PBIP rates", "Fund prices are simulated by PBIP" | change |
| `modules/assistant/faq.py` | the FAQ answers that name PBIP | change |
| `modules/loans/schemas.py` | field description "active PBIP EMIs" | change (OpenAPI description) |
| `modules/wealth/schemas.py` | descriptions "PBIP has no market data", `PRICE_SOURCE_DESCRIPTION`, "no insurer decides it in PBIP" | change (OpenAPI descriptions; the `SIMULATED` value stays) |
| Product catalogue texts the API returns (loans, wealth, recommendations) | any "PBIP" in a name, note or description shown to customers | change where it is a display string, keep where it is stored |
| **Kept:** `core/config.py` `internal_bank_code="PBIP0001"` and `payees/schemas.py` default (a stored bank code); `scheduled_service.py` lock namespace (`"PBIP"` in hex); problem `type` URIs `https://pbip.example/problems/...` (contract values); docstrings and comments; `ml/.../promotion.py` MLflow model description (not shown to users); env vars `PBIP_*`; Grafana dashboard files and uids | | identifiers, stored or contract values, or not user-facing |

The Phase 4b agent re-checks this list with a case-insensitive search before changing anything, and
records every file it touches in the progress file.
