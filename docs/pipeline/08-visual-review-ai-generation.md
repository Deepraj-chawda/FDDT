# 08 — Visual inconsistency review & AI-generation assessment

**What it detects:** whether a page *looks* edited or synthetic to a vision model — inconsistent fonts,
misaligned text, patchy colour/contrast, uneven sharpness or lighting — and whether it appears
AI-generated. It is a **secondary, judgment-based** signal that can catch what pixel forensics
miss, or corroborate them.

**Code:** `services/visual_inconsistency_service.py` (`run_visual_inconsistency_review`),
`services/llm_service.py` (`analyze_page_visual_consistency`, `PageVisualAnalysis`), run by
`tasks/visual_inconsistency_task.py`; stores `check_type = visual_inconsistency_review`.

## Algorithm

1. Render every page of the PDF (`render_pdf_pages`, 200 DPI) and encode as PNG, longest side capped at
   **1536 px** (`MAX_IMAGE_DIMENSION`). PNG, not JPEG, so glyph edges stay crisp and compression
   artifacts are not introduced.
2. Send each page to the vision model **twice, independently** (`RUNS_PER_PAGE = 2`), at
   `temperature = 0.4` — deliberately non-zero, unlike every other call in `LLMService`, so the runs are
   genuinely independent samples. Each run answers six structured questions:
   font consistency, text alignment, colour/contrast, resolution/sharpness, shadow/lighting — each a
   `VisualCategoryFinding {consistent, description, confidence, bounding_box}` — plus the
   `AIGenerationAssessment {likely_ai_generated, description, confidence}`.
3. **Corroboration rule** (the reliability mechanism): a category is reported as a real finding only if
   **both runs** flagged it, or **either** run flagged it with `high` confidence. A single low/medium
   one-off is kept but labelled "treated as noise" at severity `info`.
4. **Severity:** for a confirmed finding, `high` if both runs agreed *and* at least one was
   high-confidence, else `medium`; everything not confirmed (both runs consistent, or an uncorroborated
   one-off) is `info`. The check's `result` is `flag` if any finding is `medium` or `high`, else `pass`.
5. The task appends `metadata_forensics_correlation` (info) cross-referencing the metadata result, so a
   reviewer sees editing-tool evidence next to visual evidence. It is **not scored** (avoids double
   counting), as is `experimental_signal_notice` (an info notice always added to the result).

## Findings

| `finding` | Meaning |
|---|---|
| `visual_font_consistency` | Fonts vary within the page |
| `visual_text_alignment` | Text or table alignment is inconsistent |
| `visual_color_contrast_consistency` | Colour/contrast differs across regions |
| `visual_resolution_sharpness_consistency` | Sharpness differs across regions |
| `visual_shadow_lighting_consistency` | Shadows/lighting inconsistent |
| `ai_generation_assessment` | The page may be AI-generated/synthetic |
| `experimental_signal_notice`, `metadata_forensics_correlation` | Info only, never scored |

## Output shape (real, truncated)

```jsonc
{
  "result": "flag",                          // flag if any corroborated finding, else pass
  "details": [
    {"finding": "experimental_signal_notice", "severity": "info",
     "description": "This check uses a vision-capable language model's visual judgment, run twice independently…"},
    {"finding": "visual_font_consistency", "severity": "high", "page": 1,
     "description": "Page 1 — Font consistency: The font weight and style for the item descriptions … This is a vision-model judgment, not pixel-level analysis — a probabilistic signal, not a definitive finding.",
     "bounding_box": {"page": 1, "x": 0.05, "y": 0.3, "width": 0.9, "height": 0.15},
     "data": {
       "category": "font_consistency",
       "run_agreement": "2-of-2 runs",       // | "1-of-2 runs (high confidence)" | "consistent in both runs" | "1-of-2 runs, not corroborated" | "not flagged"
       "run_1": {"consistent": false, "confidence": "high", "description": "…", "bounding_box": {"x": 0.05, "y": 0.3, "width": 0.9, "height": 0.15}},
       "run_2": {"consistent": false, "confidence": "high", "description": "…", "bounding_box": {…}}}}
  ]
}
```

`data` keeps **both raw runs**, so a reviewer or developer can see exactly what was said and why a
finding did or did not survive. The `bounding_box` (from the first run that supplied one) is drawn as a
**dashed blue "AI-described area (approximate)"** overlay — deliberately styled to look less precise than
the red/orange pixel-forensic boxes.

## Known limitations

- **It is a language model's opinion.** Runs vary; corroboration reduces but does not remove
  hallucination. Boxes are approximate (models are poor at localisation).
- **AI-generation detection is unproven** on scanned/business documents. It is weighted lowest of any
  visual signal (weight 6) and every description says "experimental, probabilistic".
- **Every page costs two vision calls.** A 10-page PDF is 20 calls; there is no page cap on this task
  (unlike signature detection, capped at 10 pages).
- Legitimate documents made of mixed sources (a template with a pasted logo, mixed fonts) can flag.
- **Font judgement is unreliable**: the page is downscaled to 1536 px, and a few amounts retyped in a
  different font are often missed. The [font consistency](06a-font-consistency.md) check covers fonts
  deterministically on digital PDFs, and with OCR font recognition on scans.
- PDF only.

## Risk rules fed

Each rule fires only on `medium`/`high` findings (`severity_in`).

| `rule_id` | Finding | Weight | Severity |
|---|---|---:|---|
| `visual.font_inconsistency` | `visual_font_consistency` | 12 | medium |
| `visual.alignment_inconsistency` | `visual_text_alignment` | 10 | medium |
| `visual.color_contrast_inconsistency` | `visual_color_contrast_consistency` | 12 | medium |
| `visual.sharpness_inconsistency` | `visual_resolution_sharpness_consistency` | 10 | medium |
| `visual.lighting_inconsistency` | `visual_shadow_lighting_consistency` | 8 | low |
| `ai.generated_content_suspected` | `ai_generation_assessment` | 6 | low |
