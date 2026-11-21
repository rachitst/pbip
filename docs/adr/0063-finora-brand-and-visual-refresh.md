# ADR 0063: Finora brand and visual refresh of the web UI
- Status: Accepted (under owner's standing approval, 2026-11-21; the redesign itself is the owner's brief)
- Date: 2026-11-21
- Deciders: Architecture Team (amends the visual direction of ADR-0016)

## Context
The owner asked for a complete UI redesign and a rebrand of the product to **Finora** ("Smart personal
banking", assistant "Ask Finora") before the final presentation on 25 Nov 2026: a modern, clean fintech
look, light theme first, an original brand that copies no real bank.

ADR-0016 set the web architecture (React, tokens with `light-dark()`, shared primitives in `src/ui/`, one
shell) and a visual direction: the system font, navy `#00395d`, radius 4-8 px, shadows only on overlays,
nothing larger than 24 px. The architecture still holds; the visual direction is what changes. The navy is
also close to a real bank's brand colour.

Options considered:
1. **Restyle on the existing architecture:** new tokens and component styles, the same primitives and
   pages, an original logo, a self-hosted font and a small icon set.
2. **Adopt a component library** (for example MUI or Chakra): faster for new screens, but hundreds of kB
   of runtime CSS-in-JS (which the CSP `style-src 'self'` would block without nonces) and every page's
   markup rewritten.
3. **Rename only:** swap the text and keep the look. Does not meet the brief.

## Decision
Option 1.
- **Tokens:** the palette, type scale, radius and shadow in `docs/design/ui-design-system.md` replace
  ADR-0016's values in `tokens.css`. Every colour is still defined once with `light-dark()` and checked for
  contrast in both themes (text 4.5:1, control borders and chart marks 3:1).
- **Font:** Inter (variable), self-hosted from `@fontsource-variable/inter` (OFL-1.1, the woff2 files are
  bundled by Vite and served by our nginx; no request leaves the stack, and the CSP needs no change).
- **Icons:** `lucide-react` (ISC licence, tree-shaken: only the icons the app imports are bundled, each a
  small inline SVG component), so icons are consistent across the shell and pages.
- **Brand:** an original logo mark and wordmark drawn as SVG for this project (`apps/web/src/brand/`), also
  used as the favicon.
- **Theme:** light by default on a first visit; the dark toggle and its remembered choice stay.
- **Signed-out landing page** at `/` only; every other signed-out URL still shows sign-in in place
  (deep links survive), and signed-in users land where they did.
- **What does not change:** routes, hooks, API clients, validation, the auth and MFA flow, state handling,
  the API contract; the repository, domain, package names, identifiers, environment variables, compose
  service names and CI workflow names keep PBIP. Backend changes are limited to user-facing display
  strings that contain the old name.

## Consequences
- Two runtime dependencies (font files and icon components), both small, permissively licensed and
  bundled; `npm audit` covers them like the rest.
- `docs/design/ui-guidelines.md` is updated to the new visual direction; the rest of ADR-0016 (structure,
  primitives, accessibility rules, the chart conventions) still applies.
- Tests keep their roles, accessible names and labels; only tests that assert the old name or pure styling
  change, each listed in `docs/design/ui-redesign-progress.md`.
- The recorded walkthrough, the demo cut and the slide screenshots are re-made on the new UI after v1.2.0.
