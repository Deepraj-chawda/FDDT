# Case Detail Page — Full UI Specification

Reference document describing every visual element of the current "Case Detail" page (`/cases/:caseId`) in the Document Authenticator Tool, for handoff to a UI-generation tool. Built with React + TypeScript, Tailwind CSS v4, shadcn/ui (Radix primitives), lucide-react icons.

---

## 1. Design tokens (color palette)

All colors are CSS custom properties. Two are plain hex (recently changed to a "Verified Blue" brand direction); the rest are OKLCH (I give an approximate hex/Tailwind equivalent for each so a non-OKLCH tool can use them).

| Token | Value | Approx. hex | Role |
|---|---|---|---|
| `--background` | `oklch(0.985 0.003 240)` | `#FAFAFB` (near-white, faint cool tint) | Page background |
| `--foreground` | `oklch(0.18 0.04 260)` | `#1E2233` (very dark navy) | Default text |
| `--card` | `oklch(1 0 0)` | `#FFFFFF` | Card/panel backgrounds |
| `--card-foreground` | same as foreground | `#1E2233` | Text on cards |
| `--primary` | `#1D4ED8` | `#1D4ED8` (blue-700) | **Brand color.** Buttons, active nav underline, logo mark bg, header accent-line start, brand-tinted badges/tiles, focus/active states, risk-stripe-neutral fallback isn't this — see border below |
| `--primary-foreground` | `#FFFFFF` | white | Text/icons on primary-colored surfaces |
| `--secondary` | `oklch(0.955 0.01 250)` | `#EEF1F5` (very light cool gray) | Neutral pill backgrounds, secondary buttons, avatar bg |
| `--secondary-foreground` | `oklch(0.24 0.06 260)` | `#262B45` (dark navy) | Text on secondary surfaces |
| `--muted` | same as secondary | `#EEF1F5` | Subtle backgrounds (empty-state icon chips, progress-bar track, summary chip pills) |
| `--muted-foreground` | `oklch(0.5 0.03 255)` | `#767A8C` (medium gray) | Secondary/caption text throughout |
| `--accent` | `#2563EB` | `#2563EB` (blue-600) | Structural hover color (outline/ghost button hover) — same blue family as primary, not a separate hue |
| `--accent-foreground` | `#FFFFFF` | white | Text on accent hover |
| `--brand-glow` | `#F59E0B` | `#F59E0B` (amber-500) | **Decorative only** — header's bottom accent-line gradient end color. Never used for status/semantic meaning |
| `--destructive` | `oklch(0.55 0.22 27)` | ≈ `#DC2626` (red-600) | Errors, "Flag" badges, destructive borders/text, reject button |
| `--destructive-foreground` | `oklch(0.99 0 0)` | near-white | Text on solid destructive backgrounds |
| `--warning` | `oklch(0.74 0.16 75)` | ≈ `#D9A441` (amber, close to amber-500/600) | Medium-severity badges, uncertain-field markers |
| `--success` | `oklch(0.6 0.14 155)` | ≈ `#16A34A` (green-600) | "Pass" badges, clean-flag badges, low-risk badges, approve button |
| `--info` | `oklch(0.55 0.16 250)` | ≈ `#3B6FE0` (blue) | Reserved info-severity badge (rarely used) |
| `--border` | `oklch(0.9 0.012 255)` | `#E2E5EB` (very light gray) | All hairline borders |
| `--input` | `oklch(0.92 0.012 255)` | `#E7E9EF` | Form input borders |
| `--ring` | `#2563EB` | `#2563EB` | Focus ring color |
| `--radius` | `0.625rem` (10px) | — | Base corner radius (`rounded-lg` = this value; `rounded-xl` = +4px = 14px; `rounded-md` = −2px = 8px; `rounded-full` for pills/dots) |

**Semantic color usage pattern (important for consistency):** every status/severity color is applied as a **soft tint**, never solid-fill except for primary buttons:
- Badges: `background: color/15%` (or `/10%`, `/20%` for warning), `text: color` at full opacity, `border: transparent`, `border-radius: 9999px` (pill), `padding: 2px 10px`, `font-size: 12px`, `font-weight: 500`.
- Card left-border "stripes" (risk indicator, check pass/fail): 4px solid left border in the full semantic color, with the card's own thin 1px border staying neutral gray (or destructive/40% opacity when flagged) and a very faint background tint (`bg-destructive/5`) only when flagged.

**No dark mode is actually toggled in this app** (a `.dark` class variant exists in the CSS but nothing in the UI switches it) — design for light mode only, background `#FAFAFB`, cards white.

---

## 2. Typography

- **Font family:** system default stack (Tailwind's `ui-sans-serif, system-ui, sans-serif` — no custom webfont loaded). Renders as San Francisco / Segoe UI / Roboto depending on OS.
- **Monospace:** used specifically for Case ID / Case Number (`font-mono`, system mono stack) — e.g. `CASE-411482CB`.
- Type scale actually used on this page:
  - Page/case title (`h1`, case number): `18px / font-semibold / tracking-tight`
  - Document filename (`h2`): `18px / font-semibold`
  - Card titles ("Checks", "Key fields", "Extracted fields"): `16px / font-semibold`
  - Body/default text: `14px / regular`
  - Secondary/caption text (muted-foreground): `12px`
  - Micro text (timestamps, tiny labels, upload %): `11px` and `10px`
  - Badges: `12px / font-medium`
  - Tab labels: `14px / font-semibold`

---

## 3. Overall page layout

```
┌─────────────────────────────────────────────────────────────────┐
│ TOP NAV BAR (sticky, full width)                                 │
├─────────────────────────────────────────────────────────────────┤
│  ← Back to case queue                                            │
│                                                                    │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ CASE HEADER BAR (card, risk-colored left stripe)            │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌──────────┐  ┌──────────────────────────────┐  ┌────────────┐ │
│  │ LEFT      │  │ MAIN PANEL                    │  │ RIGHT       │ │
│  │ SIDEBAR   │  │ (document tabs + content)     │  │ SIDEBAR     │ │
│  │ 220px     │  │ flexible width                │  │ (Activity)  │ │
│  │ fixed     │  │                                │  │ 230px fixed │ │
│  └──────────┘  └──────────────────────────────┘  └────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

- Outer page background: `--background` (`#FAFAFB`)... actually the content area sits on `bg-muted/30` (a very faint gray wash, ~30% opacity of `--muted`) so cards (white) pop against it.
- Content max-width: `1152px` (Tailwind `max-w-6xl`), horizontally centered, `24px` side padding, `24px` top/bottom padding.
- The three-column region (sidebar / main / activity) is a flex row with `24px` gap, **stacks vertically below the `lg` breakpoint (1024px)** — sidebar, then main, then activity, full width each.
- Vertical rhythm between major blocks: `16px` gap.

---

## 4. Top Navigation Bar

Sticky header, `56px` tall, full width, `bg-background` at 95% opacity with backdrop-blur (frosted-glass effect over scrolled content). A `2px` gradient line runs along the very bottom edge: `linear-gradient(to right, --primary, --brand-glow)` — blue fading to gold.

Content is centered within the same `1152px` max-width, `24px` side padding, split into left and right groups:

**Left group:**
1. Logo mark: `28×28px` rounded-square (`rounded-md`, ~8px radius), solid `--primary` background, white shield-checkmark icon (lucide `ShieldCheckIcon`, 16px) centered, with a soft `4px` ring around it in `primary at 15% opacity` (a subtle halo/glow effect).
2. Wordmark: "Document Authenticator" — `14px font-semibold`, hidden below `sm` breakpoint (640px).
3. A thin `1px × 14px` vertical divider line (`--border` color), hidden on small screens.
4. "Case queue" nav link — `14px font-semibold`. When on the case queue route it's `--primary` colored text with a `2px` solid `--primary` bottom border; otherwise muted-gray text with a transparent bottom border (turns to normal foreground color on hover). This sits flush with the header's bottom edge (achieved via negative margin so its border sits right above the gradient accent line).

**Right group:**
1. User avatar: `28×28px` circle, `--secondary` background, initials (e.g. "SA") in `12px font-semibold`, with a `2px` primary-tinted ring (15% opacity).
2. Name + role stack (hidden below `sm`): name `14px font-medium`, role below it `12px muted, capitalized` (e.g. "Admin").
3. "Sign out" button — outline-variant button (see Button spec below), small size, with a logout icon (`LogOutIcon`, 14px) followed by the text "Sign out".

---

## 5. Case Header Bar

A single card sitting directly below the "← Back to case queue" link.

- Container: white card, `rounded-xl` (14px), `1px` border, subtle shadow (`shadow-sm`), `20px` horizontal / `16px` vertical padding, `8px` internal vertical gap between rows.
- **Risk-colored left stripe:** `4px` solid left border, colored by the case's risk tier — green (`success`) for low, amber (`warning`) for medium, red (`destructive`) for high, neutral gray (`border` color) if not yet scored.

**Row 1** (flex, space-between, wraps on narrow screens):
- Left cluster (flex, `12px` gap, wraps):
  - Case number, e.g. `CASE-411482CB` — `18px font-semibold monospace tracking-tight`.
  - Case-type badge — neutral "secondary" pill (light gray bg, dark text), e.g. "Vendor invoice".
  - Status badge — **"brand" variant** pill: `primary at 10% opacity` background, `primary` colored text, e.g. "Submitted".
  - Risk badge — semantic pill colored by tier: green "Low risk" / amber "Medium risk" / red "High risk", or plain muted text "Not yet scored" (no pill) if null.
  - Flag badge — a pill with a small solid dot (`6px` circle) + text, colored semantically: green "Clean", or red "Evidence required" / red "Mismatch".
- Right cluster — **Reviewer action buttons** (Approve / Reject / Escalate), all **disabled** (grayed out, `cursor: not-allowed`, and the whole group has a browser-native tooltip on hover: *"Reviewer actions aren't wired up yet"*):
  - **Approve**: solid green background (`--success`), white text, small button.
  - **Reject**: outline button, red border (40% opacity) + red text.
  - **Escalate**: outline button, amber border (60% opacity) + default dark text.

**Row 2:** small caption line, `12px muted-foreground`: `Submitted by {name} · {date} · {n} document(s)`.

**Row 3** (only rendered if there's something to show): a row of small pill chips, `11px font-medium`, neutral `--muted` background, rounded-full, e.g. `"2 checks flagged"`, `"1 cross-document finding"`. These are honest counts from real data — never fabricated/estimated numbers.

---

## 6. Left Sidebar (220px fixed width)

Flex column, `16px` gap between sections.

1. **Case ID block:** label "Case ID" (`12px muted`), value below in `14px font-semibold monospace`.
2. **Flag warning card** (only rendered when the case flag is NOT "clean"): a card with `1px` red border (30% opacity), red-tinted background (10% opacity), `rounded-lg`, `12px` padding, containing a small red triangle-warning icon (`AlertTriangleIcon`, 16px) + a bold red title line + a smaller red description line below it (90% opacity). Example: **"Evidence required"** / *"Only one document has been uploaded — at least 2 are needed to cross-check amount, date, and issuer across this case."*
3. **Documents list:**
   - Header: `"Documents (N)"`, `12px font-medium muted`.
   - Each document is a button-row, full width, `8px` padding, `rounded-md`, flex row with `8px` gap:
     - A `20×20px` rounded-square icon chip, color-coded by document **role**: the "claim" document (the invoice/quotation itself) gets a **blue** chip (`primary at 10%` bg, primary icon), the "evidence" document (payment proof/receipt) gets a **green** chip (`success at 15%` bg, success icon), other/pending get a neutral gray chip. Icon inside is a small file-text glyph (`FileTextIcon`, 12px).
     - Text stack: short type label (e.g. "Invoice", "Evidence", "School doc") in `14px font-medium` with a small `6px` colored status dot next to it (gray = still processing, red = has flagged checks, green = all checks passed) — below that, a `12px muted` one-line status summary, e.g. *"1 check flagged"*, *"All checks passed"*, *"Processing…"*.
     - The currently-selected document row has a light gray (`--secondary`) background fill; unselected rows show a subtle gray hover background on mouse-over.
   - **Uploading-file rows** (transient, shown only during an active upload): same row shape, gray neutral icon chip, filename, and in place of the status line: a thin `4px`-tall progress bar (gray track, blue fill, animated width) plus the percentage as tiny `10px` tabular-number text to its right.
4. **"+ Add document" button:** full-width outline button with a plus icon, disabled + label changes to "Uploading…" during an active upload. Any upload error appears as small red text below it.

---

## 7. Main Panel (flexible width, center column)

### 7a. Document header row
- Flex row, space-between: filename (`18px font-semibold`, truncates with ellipsis) + optionally a small red rounded-full pill next to it reading `"{N} flagged"` — versus a "View file" outline button on the right (opens the raw file in a new tab).
- Below that: file size, tiny `12px muted` caption (e.g. "354.4 KB"), pulled up slightly (negative margin) to sit close to the header.
- If processing failed: a red-tinted error banner (light red bg, red border, red text, `rounded-md`, padding) showing the raw backend error message.
- If still processing (pending/processing status): plain muted-gray sentence: *"Still processing — checks and extracted fields will appear once this finishes."*

### 7b. Tab strip (only shown once processing is complete)
Horizontal row of text tabs separated by a full-width `1px` bottom border (like a browser tab bar), `20px` gap between tab labels, `8px` bottom padding on each:
- **Overview** (default active on every document switch)
- **Checks (N)** — N = number of automated checks
- **Fields**
- **Cross-document (N)** — only shown at all when the case has 2+ documents

Active tab: `--primary` colored text + a `2px` solid primary underline sitting flush on the strip's own border. Inactive tabs: muted-gray text, transparent underline, darkens to normal text color on hover.

### 7c. Overview tab — 2-column grid (stacks to 1 column below `sm`, 640px)

**Left card — "Checks":** a plain card (white, bordered, rounded) with a divided list (`divide-y` — thin horizontal rule between rows) of mini check rows. Each row: small check-circle icon (green, `CheckCircle2Icon`) or triangle-alert icon (red, `AlertTriangleIcon`), the check's name, and its result badge on the right (Pass/Flag/N/A), all in a compact `14px` row with `6px` vertical padding — non-expandable, just a glance summary. If the case has 2+ documents, a synthetic "Cross-document consistency" row is appended at the end using the same styling.

**Right card — "Key fields":** same card styling, flex-column list of just 3 fields (Issuer, Amount, Date), each with a small muted-gray icon next to its label (building icon for Issuer, dollar-circle icon for Amount, calendar icon for Date) — see Field Cell spec below.

### 7d. Checks tab

1. **One card wrapping everything**, titled "Checks".
2. **PDF viewer** (only rendered for `.pdf` documents) sits at the top of the card body — see full spec in section 9 below.
3. **One expandable check card per automated check**, stacked vertically with `8px` gap. Each card:
   - `rounded-lg`, `1px` border, **`4px` colored left border** (green if passed, red if flagged), `16px` horizontal / `12px` vertical padding.
   - When flagged: the whole card also gets a very faint red background wash (`destructive at 5%`) and the thin border turns reddish (40% opacity) — not just the left stripe.
   - Header row (always visible, click to expand/collapse — native disclosure-triangle-free `<details>/<summary>` pattern): status icon (green check-circle or red alert-triangle, 16px) + the check's human name (e.g. "Metadata forensics", "Error level analysis", "Copy-move forgery detection", "Date / amount / reference validation", "Issuer verification") in `14px font-medium`, and the result badge (Pass/Flag/N/A/Error/Skipped) right-aligned.
   - **When expanded**, body content varies by what the check returned:
     - **A list of findings** (used by metadata forensics, ELA, copy-move): each finding is its own small nested collapsible row (`rounded-md`, thin border, `10px` padding) — bold finding name + a colored severity badge (info=gray, low=gray, medium=amber, high=red) in the header, and a muted description sentence plus optional key/value data pairs when expanded. High/medium severity findings start pre-expanded; info/low start collapsed.
     - **A sub-check breakdown** (used by field validation): a flex row per sub-check — label on the left, a short reason sentence + a small result badge on the right.
     - **A flat key/value list** (used by issuer verification): divided rows, label left / value right.
     - **Empty state** (new addition): when a check has genuinely nothing to list (a clean "Pass" or an "N/A" result), instead of a blank body it now shows one line of plain muted `14px` text explaining *why*, e.g.:
       - ELA, Pass: *"This page has an embedded image and was analyzed — no region showed an abnormally high JPEG recompression error."*
       - ELA, N/A: *"This page has no embedded raster image — recompression-error analysis only applies to photographic/scanned content, not born-digital text or vector art."*
       - Copy-move, Pass: *"No duplicated regions were found — nothing on this document appears to have been copied and pasted elsewhere on the same page."*
       - Any other check with nothing to show: generic fallback *"No additional detail was recorded for this check."*

### 7e. Fields tab
One card, "Extracted fields" title + a small muted subtitle line showing the classified document type and confidence %, e.g. *"Commercial invoice · 92% confidence"*.

Body: a 2-column grid (`24px` column gap, `16px` row gap) of **Field Cells**:
- Each cell: tiny label row (`12px muted`, with a small icon before it — building=Issuer, hash=Reference #, calendar=Date, dollar-circle=Amount), then the value in `14px font-medium` (or an em-dash in muted gray if null), with an optional small amber "uncertain" mini-badge next to low-confidence values (hover shows a tooltip with the exact confidence %). Some fields (Date, Amount) also show a tiny `11px` "as written: ..." sub-line showing the original raw extracted text when it differs from the normalized value.
- Core 4 fields always shown: Issuer, Reference #, Date, Amount. If the document has tax breakdown data, 3 more appear: Subtotal, Tax amount, Tax rate.
- If there are additional/custom extracted fields beyond the core set, a thin top border + "Additional fields" label (`12px muted`) introduces a second grid of the same field-cell style.
- Arabic-language field *values* render right-to-left (`dir="auto"`) while the rest of the UI stays English/LTR.

### 7f. Cross-document tab
Same expandable-card visual treatment as one check card in the Checks tab — colored left stripe (red if any non-low-severity finding exists, green otherwise), header "Cross-document consistency" + result badge, body lists each finding as a row (description left, severity badge right) or a muted "No cross-document issues found for this document" sentence if empty.

---

## 8. Right Sidebar — Activity Timeline (230px fixed width)

- Header label: "Activity", `12px font-medium muted`.
- A vertical list (`12px` gap between items) of timeline entries, each: a small `6px` colored dot (blue for case-submitted, gray for routine processing events, red for failures) offset slightly above the text baseline, then a text stack:
  - Event label in `12px font-medium` (e.g. "Case submitted", "Document uploaded", "OCR / extraction complete", "Metadata forensics complete", "Cross-document check complete")
  - Optional one-line detail in `11px muted` (a filename, or a finding count like "2 findings")
  - Timestamp + actor name in `11px muted`, e.g. *"Sep 14, 8:49 PM · Seed Admin"*
- No connecting vertical line between dots — just consistent left-alignment and spacing implies the sequence.
- Loading state: "Loading…". Empty state: "Nothing logged yet."

---

## 9. PDF Highlight Overlay Viewer (inside the Checks tab)

A self-contained bordered card-like block (`rounded-lg`, `1px` border, `12px` padding) sitting above the check cards:

- **Header row:** left side has page-navigation — a small square icon-button (chevron-left) / "Page X of Y" text (`12px muted, tabular-nums`) / chevron-right icon-button, each button disabled at the first/last page. Right side (only when a check with region data is currently expanded) shows a small **legend**: colored `10×10px` rounded-square swatch + label, e.g. a red swatch "ELA tampering" and/or an amber/orange swatch "Copy-move" — both can appear together.
- **Below that:** the actual rendered PDF page (white, bordered, centered, rendered at a fixed ~560px display width, scaled proportionally) sitting inside a `position: relative` frame.
- **Highlight boxes:** semi-transparent colored rectangles (`2px` colored border + ~15–25% opacity fill of the same color) absolutely positioned over the PDF render, sized/positioned using normalized (0–1 fraction of page width/height) coordinates converted to percentages — so they scale correctly regardless of the PDF's real resolution. Red for an ELA finding's region, amber/orange for a copy-move finding's region. Boxes appear only while their originating check card is expanded; collapsing the check removes that box immediately (no annotated image is ever baked into a file — the highlight is drawn live in the browser).
- Load-failure state: plain red text "Couldn't load this file for preview." The whole viewer is wrapped in an error boundary so a PDF-loading crash can't take down the rest of the page.

---

## 10. Buttons — variant reference

| Variant | Look |
|---|---|
| Default (solid) | `--primary` background, white text, `shadow-xs`, darkens slightly on hover |
| Outline | White background, `1px` border, `shadow-xs`, fills with `--accent` blue + white text on hover |
| Secondary | Light-gray (`--secondary`) background, dark text |
| Destructive | Solid red background, white text |
| Ghost | Transparent, fills `--accent` blue on hover |
| Disabled (any) | 50% opacity, no pointer events, cursor unaffected by hover states |

Sizes used on this page: default (`36px` tall), small/`sm` (`32px` tall, used almost everywhere on this page), icon-only square (`36×36px`, used for the PDF page-nav chevrons at a smaller custom `28×28px`). Corner radius `rounded-md` (8px) on all buttons.

---

## 11. Icons (lucide-react, all outline-style, stroke-based)

`ShieldCheckIcon` (logo), `LogOutIcon` (sign out), `AlertTriangleIcon` (flag/warning states), `CheckCircle2Icon` (pass states), `FileTextIcon` (document row icon), `PlusIcon` (add document), `Building2Icon` (Issuer field), `CalendarIcon` (Date field), `CircleDollarSignIcon` (Amount field), `HashIcon` (Reference # field), `ChevronLeftIcon` / `ChevronRightIcon` (PDF page nav).

---

## 12. Realistic example content (for grounding generated copy/data)

```
Case: CASE-58E1ADE3 · Vendor invoice · Submitted · Medium risk · Evidence required
Submitted by Seed Admin · Sep 14, 2026 · 2 documents
Chips: "2 checks flagged"

Document: tampered_test_invoice3.pdf (354.4 KB)
Issuer: AL-SUWAIDI NUCLEAR & PRECISION INSTRUMENTATION LLC
Amount: 189,000.00 AED
Reference #: INV-ENEC-2026-1049
Date: 2026-08-25

Checks:
  Metadata forensics — Pass
  Error level analysis — Pass
  Copy-move forgery detection — Flag
    Finding: "Copy Move Cluster" — High severity
    "Page 1: found a cluster of 35 matching feature pairs, consistent with
     one region of this page having been copied and pasted elsewhere on
     the same page."
    Matched Pairs: 35
  Date / amount / reference validation — Pass
  Issuer verification — Pass

Activity:
  Case submitted — Sep 14, 8:00 PM · Seed Admin
  Document uploaded — tampered_test_invoice.pdf — Sep 14, 8:00 PM · Seed Admin
  OCR / extraction failed — Sep 14, 8:00 PM
  tampering_checks_completed — Sep 14, 8:00 PM
```

---

## 13. Overall visual character (for style guidance)

Clean, dense, data-forward "internal review tool" aesthetic — closer to a fraud-ops/compliance dashboard than a marketing product. Generous use of soft-tinted status colors rather than solid fills (except primary buttons). Hairline borders and subtle shadows define structure, not heavy drop-shadows or gradients (the only gradient in the whole page is the thin 2px header accent line). Information density is high but organized into clear card boundaries and consistent left-stripe color-coding so a reviewer can scan risk/status at a glance without reading every word. Whitespace is tight but not cramped — 12–16px is the dominant internal spacing unit.
