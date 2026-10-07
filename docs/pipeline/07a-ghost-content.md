# 07a — Deleted / replaced content (ghost text)

**What it detects:** on a scan that was **converted to editable text** (Acrobat "Edit scanned
document", iLovePDF and the like), text that was **deleted** or **shortened** after the conversion.

**Code:** `services/forensics/ghost_content.py` (`analyze_ghost_content`, `exclude_signature_regions`),
run by `tasks/tampering_checks_task.py` (`run_tampering_checks`, same task as ELA / copy-move since it
reads the same bytes). One `document_checks` row: `ghost_content`.

## Why it works

A converter erases the text pixels from the scan and redraws every word as live text on top. The erasure
is never perfect: a faint **ghost** of each word stays in the background image.

| What the background shows | Meaning |
|---|---|
| ghost **with** live text on top | normal — the converted word |
| ghost with **no** live text on top | content that was there at conversion and was **deleted** since |
| ghost much **wider** than the live text on top | the line was **shortened or rewritten** |

## When it runs

Only on pages built that way (`page_structure.PageStructure.vector_text_over_image`, from PyMuPDF's
drawing order): one image covering **≥ 90 %** of the page (it may overhang the page edges, as converters
place it — e.g. `-10,-7,605,849` on a 595 × 842 page) drawn **before** the text, and **≥ 15** visible
text runs (render mode ≠ 3) spanning ≥ 50 % of the page height — **whatever the fonts are called**
(a converter's `-NNNN` subsets or ordinary `ABCDEF+` subsets).

A plain scan (no text layer), a searchable scan (OCR text invisible, render mode 3) and a born-digital
page (no page-sized image) → result **`not_applicable`**, with the reason in `data.reason` ("no text layer
(a plain scan)", …) — shown on the case page and in the report's coverage table ("Not applicable: no text
layer (a plain scan)").

Documents processed before this check existed: `python -m scripts.backfill_ghost_content` (all of them, or
`--case CASE-…`) runs it on every PDF without a result, and re-scores a case that flags. A converted page whose scan is placed rotated
or skewed is also reported `not_applicable` (not handled).

## Algorithm (background image at its native resolution, 8-bit gray)

1. **Residue.** Morphological **black-hat** with a kernel of ½ the median text height: how much darker
   each pixel is than the paper around it, for marks thinner than a stroke. Broad shading and uneven
   illumination are part of the "paper".
2. **Not searched:** where the paper itself is not white — shaded cells and bands (often a scanner's
   halftone dots), photos, logos left in the scan: median tone over ~2 text heights more than 12 levels
   darker than the page's white. A ghost cannot be told from that texture.
3. **Ghost band.** Residue between the page's noise floor (median over empty 64-px tiles of their 99th
   percentile, at least 4) and **70** (stronger is ink still in the image — rules, logos, stamps,
   signatures — removed with a 2-px halo). Ruled lines are removed (bridged across short breaks, then
   long thin runs; a band as tall as letters is text, not a rule), as are specks < 6 px and components
   much flatter than a glyph.
4. **Lines.** The ghost mask is smeared horizontally (0.6 × text height) into lines; kept when 0.5–2.5 ×
   the median text height tall, ≥ 25 pt wide, aspect ≥ 1.8 and 3–60 % ink.
5. **What may sit on a ghost:** visible text spans (+3 pt; lines skewed by up to ~6° count), other images
   (+2 pt — converters lift logos, stamps and signatures into their own images), filled vector shapes,
   and a 4 % page margin.
6. **Classify each ghost line** (after dropping a line with more ink still in the image than ghost — the
   faint edge of a signature or of print the converter left as image — and a line ≥ 1.5 × stronger than
   the page's own erased text, i.e. a smudge):
   - **replaced line** (`ghost_replaced_line`): its *uncovered* ghost runs on past the live text on its
     row (or before it) by > 30 % of the end word and > 15 pt, into empty space (not up to the next word),
     and that uncovered part itself looks like text (≥ 25 pt, ≥ 50 % of its columns marked, ≥ 4 glyph-height
     marks). Text the converter could not map to characters is skipped (its width means nothing).
   - **orphan line**: < 20 % covered, no live word on its row within 4 text heights, and text-like
     (≥ 3 glyph-height marks). Orphan lines less than 1.5 line heights apart, side by side within 3 line
     heights (a label column and its value column), merge into a **block**; a block needs one line ≥ 40 pt.
7. **Show-through** (when OCR is available): each orphan block's enhanced crop is read as is and mirrored
   (Azure Document Intelligence Read). If the mirrored reading is real text (≥ 4 characters, confidence
   ≥ 0.5) and clearly better (× 1.5), it is the back of the page showing through: `ghost_show_through`,
   info, not scored. The as-is reading is kept as the reviewer's hint — best effort; a heavily
   compressed scan often gives none.
8. **Vision-model hint** (when an LLM with vision is configured): for each deleted block, the trace and a
   tiny layout thumbnail of the page (too small to read — the model must not take the surrounding text
   for the block's) go to the vision model **three times** (`guess_erased_content`, temperature 0.4).
   Only what at least two answers agree on is kept (`data.vision_hint`: `kinds`, `heading`,
   `legible_words`, `answers_agree`); confidence is "medium" only when the answers agree on the kind
   and read something consistently, else "low". On CASE-39CB18BF the answers disagree (bank details /
   a note about fees), so it shows as a low-confidence guess. Never scored, never evidence.
9. **Signatures and stamps:** a finding lying ≥ 30 % inside a detected signature or stamp region is set
   aside (`ghost_near_signature`, info). Applied when the ghost check runs if detection has finished, else
   when detection finishes (`refilter_ghost_content`, called from `signature_detection_task`).

## Findings

| Finding | Severity | Meaning |
|---|---|---|
| `ghost_content_scope` | info | Pages searched (noise floor and erased-text level per page), or why not applicable |
| `ghost_deleted_block` | high | A block of faint text with no live text on top: `data.lines`, `data.box_pt`, `data.hint` |
| `ghost_replaced_line` | medium | A line's trace runs on past its live text: `data.text`, `data.overhang_pt` |
| `ghost_show_through` | info | An orphan block that reads better mirrored — not scored |
| `ghost_near_signature` | info | Set aside: inside a detected signature or stamp — not scored |

Every located finding carries a normalized `bounding_box` and `data.crop_png_base64` — the scan in that
box, contrast-stretched (darkest 0.5 % black, the paper's median tone white) so the trace shows. The case
page draws deleted / shortened content as a **solid teal** box and shows the crop under the finding; the
report draws the same box (Section 9) and adds a page of crops after the annotated page.

`result` is **`flag`** when any finding is medium or high, **`pass`** otherwise, **`not_applicable`** as
above. In the report's coverage table a check that was not applicable to any document reads
"Not applicable".

## Risk rules fed

| Rule | Finding | Weight | Severity |
|---|---|---|---|
| `content.deleted_ghost_block` | `ghost_deleted_block` (high) | 25 | high |
| `content.replaced_ghost_line` | `ghost_replaced_line` (medium, high) | 10 | medium |

Each fires at most once per document (the reason lists every block / line).

## Reference results

| Case | Result |
|---|---|
| CASE-39CB18BF (Ajyal, `6f277532…`) | **flag**: a 6-line block deleted under the fee table (x 30–270, y 388–480 pt — the bank-transfer details); the "Registration Fees" row's trace runs ~90 pt past its text |
| CASE-1CEDFDBF / CASE-F6F5FE76 (British Orchard, `895a259e…`) | pass — ghosts under the converted text only; the shaded cells, the footer the converter garbled and the stamp give nothing |
| CASE-B70765EC (ILM Academy, `14926b7f…`) | pass — no false orphans from the shaded rows |
| CASE-DE627FA2, CASE-DB43653A (MFP scans), CASE-4B297080 (Word) | not applicable |

## Known limitations

- **The student row of CASE-39CB18BF is not reported as shortened.** Its trace runs only ~6 pt past
  "PRE-KG-NA" (the words underneath are spaced differently from the live words, but are about as long);
  converter re-typesetting routinely differs by that much, so it cannot be told apart. The retyped name,
  ID and class are caught instead by the font check: they sit in a second embedded copy of the bold font
  (`font_subset_split`, [06a](06a-font-consistency.md)).
- Shaded areas are not searched, so content deleted from a shaded cell is missed.
- The hint OCR rarely reads anything on a heavily compressed scan; the crop is the evidence.
- A scan placed rotated / skewed (as an image transform) is not handled.
