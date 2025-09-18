## Faxbot Brand Colors & Adoption Plan

Goal: a unified blue-centric palette that works in both dark and light modes across iOS, Admin Console (MUI), Electron, and the docs site.

### Palette (HEX)

- Primary (medium blue): `#2563EB` (Blue 600)
- Primary (light tint): `#60A5FA` (Blue 400)
- Primary (dark/navy): `#1E3A8A` (Blue 900)
- Background (light): `#F8FAFC`
- Background (dark): `#0B1220`
- Surface (light): `#FFFFFF`
- Surface (dark): `#0F172A`
- Text (light on light bg): `#0F172A`
- Text (light on dark bg): `#E2E8F0`

Accessibility notes
- On dark surfaces: prefer the lighter tint (`#60A5FA`) for icons/accents.
- Maintain WCAG AA contrast (≥4.5:1 for normal text). Avoid placing `#1E3A8A` text on `#0F172A` surfaces.
- Keep red/green for status (error/success) consistent; do not recolor them as brand blue.

### iOS App (this repo)

- Colors added in `ios/FaxbotApp/Resources/Colors.xcassets`:
  - `BrandPrimary` (light `#2563EB` / dark `#60A5FA`)
  - `BrandBackground` (light `#F8FAFC` / dark `#0B1220`)
  - `BrandSurface` (light `#FFFFFF` / dark `#0F172A`)
- Global tint: `FaxbotApp.swift` sets `.tint(Color.brandPrimary)`.
- Use `Color.brandPrimary` for accents; rely on system materials for major surfaces.

### Admin Console (MUI, development branch)

- File: `api/admin_ui/src/theme/themes.ts`
- Set palette:
  - `primary.main = #2563EB`
  - `primary.light = #60A5FA`
  - `primary.dark = #1E3A8A`
  - Background: `default = #0B1220` (dark), `paper = #0F172A` (dark). Light theme: `default = #F8FAFC`, `paper = #FFFFFF`.
  - Text: `primary = #E2E8F0` (dark), `secondary = #CBD5E1`; light theme: `primary = #0F172A`, `secondary = #334155`.
- Do not hard-code colors in components; use theme tokens.
- Validate contrast in dark mode for any custom chip/button backgrounds.

### Electron (Admin UI shell)

- Rely on MUI theme from the renderer for content.
- For native title bars or overlays, adopt the same hex values above.
- Avoid custom CSS; use MUI tokens for coherence.

### Docs site (faxbot.net)

- Define CSS variables in the site theme (root):
  - `--brand-primary: #2563EB;`
  - `--brand-primary-light: #60A5FA;`
  - `--brand-primary-dark: #1E3A8A;`
  - `--brand-bg: #0B1220;`
  - `--brand-surface: #0F172A;`
  - `--text-primary: #E2E8F0;`
- In light mode, swap `--brand-bg` → `#F8FAFC`, `--brand-surface` → `#FFFFFF`, `--text-primary` → `#0F172A`.
- Update links, buttons, and callouts to use `--brand-primary` variants.

### Liquid Glass (iOS 26) guidance

- Prefer system materials (`.ultraThinMaterial`, `.thinMaterial`) for surfaces; layer brand color via `.tint(Color.brandPrimary)` or icons.
- Avoid solid brand fills on large backgrounds in dark mode; use tints/accents, not slabs of navy.
- Keep icons/vector assets compatible with “clear mode”—no opaque boxes; use template rendering where possible.

### Rollout plan

- iOS: done in this branch; adjust AppIcon when finalized.
- Admin Console: update `themes.ts` and verify chips/cards in dark mode.
- Electron: inherit renderer theme; avoid OS-level custom colors.
- Docs: add CSS variables in the site repo and apply to components.

