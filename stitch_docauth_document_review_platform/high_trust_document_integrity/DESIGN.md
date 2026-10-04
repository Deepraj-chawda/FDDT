---
name: High-Trust Document Integrity
colors:
  surface: '#f8f9ff'
  surface-dim: '#cbdbf5'
  surface-bright: '#f8f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#eff4ff'
  surface-container: '#e5eeff'
  surface-container-high: '#dce9ff'
  surface-container-highest: '#d3e4fe'
  on-surface: '#0b1c30'
  on-surface-variant: '#45474d'
  inverse-surface: '#213145'
  inverse-on-surface: '#eaf1ff'
  outline: '#75777e'
  outline-variant: '#c5c6ce'
  surface-tint: '#525e79'
  primary: '#000000'
  on-primary: '#ffffff'
  primary-container: '#0e1b32'
  on-primary-container: '#7784a0'
  inverse-primary: '#b9c7e5'
  secondary: '#0051d5'
  on-secondary: '#ffffff'
  secondary-container: '#316bf3'
  on-secondary-container: '#fefcff'
  tertiary: '#000000'
  on-tertiary: '#ffffff'
  tertiary-container: '#002113'
  on-tertiary-container: '#009668'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d7e2ff'
  primary-fixed-dim: '#b9c7e5'
  on-primary-fixed: '#0e1b32'
  on-primary-fixed-variant: '#3a4760'
  secondary-fixed: '#dbe1ff'
  secondary-fixed-dim: '#b4c5ff'
  on-secondary-fixed: '#00174b'
  on-secondary-fixed-variant: '#003ea8'
  tertiary-fixed: '#6ffbbe'
  tertiary-fixed-dim: '#4edea3'
  on-tertiary-fixed: '#002113'
  on-tertiary-fixed-variant: '#005236'
  background: '#f8f9ff'
  on-background: '#0b1c30'
  surface-variant: '#d3e4fe'
typography:
  display-hero:
    fontFamily: Plus Jakarta Sans
    fontSize: 40px
    fontWeight: '700'
    lineHeight: 48px
    letterSpacing: -0.02em
  display-hero-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 30px
    fontWeight: '700'
    lineHeight: 38px
    letterSpacing: -0.01em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
  stat-metric:
    fontFamily: Plus Jakarta Sans
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.03em
  eyebrow-badge:
    fontFamily: Plus Jakarta Sans
    fontSize: 11px
    fontWeight: '700'
    lineHeight: 16px
    letterSpacing: 0.12em
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  label-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 13px
    fontWeight: '600'
    lineHeight: 18px
  label-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
  code-mono:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1.5rem
  gutter-sm: 1rem
  margin: 2.5rem
  margin-mobile: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 1rem
  space-lg: 1.5rem
  space-xl: 2rem
  space-2xl: 3rem
---

## Brand & Style

The brand personality centers on forensic rigor, unassailable security, and operational clarity. Designed for fraud analysts, compliance officers, and enterprise risk teams, the UI establishes an immediate sense of institutional reliability and transparency.

The aesthetic blends **Modern Enterprise Minimalist** precision with **Soft Tactical Layering**:
- A luminous, cool lavender-blue canvas delivers calm cognitive ergonomics during high-stakes investigations.
- Crisp white elevated surfaces function as forensic paper trays, organizing complex metadata, multi-signal evidence, and audit trails without visual fatigue.
- Deep navy establishes decisive, stable authority for primary triggers, while vibrant royal blue and triple-state risk tokens (emerald, amber, crimson) direct immediate attention to explainable anomalies and validation checkpoints.

## Colors

The palette operates in high-clarity light mode to evoke paper verification and sterile forensic environments:

- **Canvas Background:** `#EDF2FA` (and sub-surface gradient tint `#E9EFF8`) acts as the cool, non-glare foundation.
- **Card Surfaces:** `#FFFFFF` with precise border demarcation `#E2E8F0`.
- **Primary Navy (`#0B1930` / `#101E38`):** Anchors navigation headers, primary execution actions (`New upload`, `Start integrity analysis`), and high-importance typographic anchors.
- **Accent Electric Blue (`#2563EB`):** Dedicated to active navigational tabs, highlighted metadata glyphs, and secondary telemetry badges.
- **Risk Triad:**
  - `Risk Low / Verified`: `#10B981` (Emerald green). Used for hash confirmations, clean registry matches, and low-risk badges.
  - `Risk Medium / Warning`: `#F59E0B` (Amber). Flags metadata revisions, cross-document timeline inconsistencies, and moderate threshold drifts.
  - `Risk High / Critical`: `#EF4444` (Crimson). Reserved for signature glyph mismatches, manual escalations, and severe anomaly scores.
- **Neutrals & Muted Tones:** Primary copy uses `#0F172A`, secondary metadata employs `#64748B`, and subtle inset borders rely on `#E2E8F0`.

## Typography

The typography system pairs **Plus Jakarta Sans** for structural hierarchy and geometric authority with **Inter** for dense, fatigue-free data comprehension.

- **Uppercase Eyebrows:** Small, spaced-out caps (`11px`, `weight: 700`, `letter-spacing: 0.12em`) sit above primary titles (e.g., `CASE INVESTIGATION`, `OPERATIONAL DASHBOARD`, `EVIDENCE INTEGRITY`) in active blue (`#2563EB`) or slate (`#64748B`) to immediately categorize context.
- **Headings:** Set in bold weights with tight negative tracking (`-0.02em`) to command attention.
- **Body & Data Rows:** Inter ensures high legibility on small-print findings, metadata timelines, and microcopy.
- **Hashes & Forensic IDs:** Rendered in `JetBrains Mono` or tabular numerals to maintain columnar alignment during side-by-side document cross-referencing.

## Layout & Spacing

The layout is built on a 12-column fluid grid system bounded by a max width of 1440px to preserve density and analytical visibility on ultra-wide desktop workstations.

- **Desktop (1280px+):** Outer margins of `2.5rem`, gutters of `1.5rem`. Side-by-side forensic layouts balance a 7-column primary document viewer with a 5-column explainable findings panel. Top metric summaries span a 4-card horizontal flex grid.
- **Tablet (768px - 1279px):** 8-column layout with `1.5rem` margins. Side panels stack beneath primary evidence displays, and horizontal stat tiles collapse into a 2x2 grid.
- **Mobile (<768px):** Single-column layout with `1rem` margins and `space-md` card gaps. Evidence zoom viewports collapse into full-width carousel blocks.

## Elevation & Depth

Visual hierarchy is maintained through dual-layer ambient diffusion over the lavender canvas rather than harsh directional drop shadows:

- **Level 0 (Canvas Base):** Solid or soft vertical linear gradient (`#EDF2FA` to `#E9EFF8`).
- **Level 1 (Default Forensic Cards):** Pure `#FFFFFF` surface with a crisp structural hairline border `1px solid #E2E8F0` and diffuse ambient soft shadow: `0 8px 30px -4px rgba(11, 25, 48, 0.04), 0 2px 6px -1px rgba(11, 25, 48, 0.02)`.
- **Level 2 (Active Modals & Hover States):** `0 16px 40px -6px rgba(11, 25, 48, 0.08), 0 4px 12px -2px rgba(11, 25, 48, 0.03)` with border tint shift toward `#CBD5E1`.
- **Level 3 (Floating Pill Controls & Overlays):** Borderless or micro-bordered pill structures elevated with `0 10px 25px -3px rgba(11, 25, 48, 0.12)`.

## Shapes

The design system uses generous, progressive curvature to humanize data-heavy enterprise screens while maintaining modular discipline:

- **Cards & Data Containers:** Standardized on `rounded-2xl` (16px) to `rounded-3xl` (24px) for major outer modules (e.g., Document Preview containers, Signal panels, and File Upload zones).
- **Buttons & Interactive Tags:** 
  - Primary execution buttons use smooth `rounded-xl` (12px) or full pill `rounded-full` for utility chips (e.g., Live feed badges, counter status pills).
- **Dropzones & Nested Evidence Insets:** Sub-containers feature `rounded-xl` (12px) with dashed `1.5px` borders in `#CBD5E1`.

## Components

### Buttons & Action Triggers
- **Primary Action:** Solid deep navy (`#0B1930`), white text, `rounded-xl`, subtle hover transition to `#101E38` with inner top highlight (`box-shadow: inset 0 1px 0 rgba(255,255,255,0.15)`).
- **Secondary / Evidence Action:** Crisp white `#FFFFFF` surface, `1px solid #E2E8F0`, navy text `#0B1930`, hover fill `#F8FAFC`.
- **Destructive / Escalation Action:** High-urgency crimson red (`#EF4444`), crisp white text, `rounded-xl`.

### Navigation Tabs & Bar
- Encapsulated floating white header bar (`h-16`, `rounded-full` or flush top with `border-b border-[#E2E8F0]`). Active tabs receive a soft tint fill (`#EFF6FF`), vibrant blue text (`#2563EB`), and a bold glyph icon.

### Stat Cards (Horizontal KPI Group)
- Elevated white cards with subtle top border highlight. Displays label in `body-sm` (`#64748B`), prominent numeric figure in `stat-metric` (`#0F172A` or state-tinted `#EF4444`), and delta micro-text (e.g., `+18 since 09:00`, `awaiting an analyst`).

### Risk Scoring Indicator
- Dual representation: Giant numeric score (`headline-lg` in crimson, amber, or green) accompanied by an adjacent pill tag (`HIGH RISK`, `MEDIUM`, `LOW`) and a 6px continuous track bar split into progress thresholds (`0-39` green, `40-69` amber, `70-100` crimson).

### Explainable Signal List Items
- Stacked rows inside white panels. Each item incorporates:
  - Left colored circular status dot (6px to 8px) in green, amber, or crimson.
  - Signal title in `Plus Jakarta Sans` semi-bold (`#0F172A`).
  - Explainable context subline in `Inter` muted regular (`#64748B`).
  - Right-aligned differential weight token (e.g., `+31`, `+22`, `-8`) indicating contribution to risk.

### Document Evidence Preview Zone
- Light-slate inset viewport (`#F1F5F9`) housing the document render, equipped with interactive floating zoom/pan controls, OCR confidence tags (`OCR 96.4%`), and bounding box visualizers highlighting tampered areas with glowing translucent overlays (`rgba(37, 99, 235, 0.15)` with `#2563EB` border).

### Audit Trail & Timeline Insets
- Bottom horizontal ribbon layout segmented by vertical keyline dividers (`#E2E8F0`). Each milestone displays a monospace timestamp (`09:41`), milestone title (`Analysis complete`), and resolution narrative.