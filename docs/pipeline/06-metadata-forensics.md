# 06 — Metadata forensics

**What it detects:** signs a PDF was edited, re-exported or assembled after it was created: editing
software fingerprints, inconsistent or missing timestamps, incremental-save revisions, XMP edit history
that contradicts the file, orphaned objects, embedded JavaScript, and a stripped metadata block.

**Code:** `services/forensics/metadata_forensics.py` (`analyze_pdf_metadata`), run by
`tasks/metadata_forensics_task.py` (`run_metadata_forensics`); stores `check_type = metadata_forensics`.

## What is read

Using **pikepdf** on the PDF's own object model (never a page render):

- the **Info dictionary** (`/Title /Author /Creator /Producer /CreationDate /ModDate`);
- the separate **XMP metadata stream**, including custom namespaces and the `xmpMM:History` revision
  chain;
- the **document ID chain** (`/ID`);
- the file's real **incremental-update / xref-revision structure** (`_walk_xref_chain`);
- **reachability**: which objects are unreachable from `/Root`/`/Info` (orphans);
- JavaScript / `OpenAction`, optional content groups, and digital-signature fields.

## Findings

Every finding is `{finding, severity, description, data?}`. `severity` is `info | low | medium | high`.
`info` findings are context (they show the raw metadata) and never affect the result.

| `finding` | Severity | Meaning |
|---|---|---|
| `pdf_unreadable` | high | The file could not be opened as a PDF |
| `metadata_entirely_stripped` | high | No Info and no XMP metadata at all |
| `editing_software_detected` | high | Producer/Creator/`softwareAgent` matches an image/graphics editor (`METADATA_FORENSICS_EDITING_SOFTWARE_NAMES`: photoshop, gimp, illustrator, affinity, coreldraw, paint.net, pixlr, canva, inkscape) |
| `pdf_editor_detected` | medium | Producer/Creator names an online or desktop PDF editor (`METADATA_FORENSICS_PDF_EDITOR_NAMES`: iLovePDF, Smallpdf, Sejda, PDFescape, PDF-XChange Editor, PhantomPDF / Foxit PDF Editor, Nitro Pro, PDFelement, Soda PDF, pdfFiller, DocHub, PDF Candy). One finding per tool. Print drivers of the same vendors are not listed |
| `mod_date_after_creation_date` | high | ModDate later than CreationDate by more than `METADATA_FORENSICS_MOD_DATE_THRESHOLD_SECONDS` (default 1 s — only jitter tolerance; being modified at all is the anomaly) |
| `info_xmp_creation_date_mismatch` | high | Info and XMP creation dates differ by > 1 minute |
| `info_xmp_mod_date_mismatch` | high | Info and XMP modification dates differ by > 1 minute |
| `info_xmp_producer_mismatch` | medium | Info `/Producer` ≠ XMP producer |
| `incremental_updates_present` | medium (1 update) / high (> 1) | The file has been saved incrementally |
| `history_editing_tool_anomaly` | medium | An XMP history entry's `softwareAgent` names an image editor or a PDF editor |
| `history_scanned_document_edited` | high | An XMP history entry's action is `editedScannedDoc` — Acrobat's "Edit scanned document": the scan was converted to editable text and edited |
| `history_edit_after_final_date` | high | An XMP history event is later than the file's modified date (+1 min) |
| `history_shorter_than_revisions` | medium | Fewer history entries than xref revisions |
| `history_absent_despite_revisions` | medium | Revisions exist but the history is empty |
| `orphaned_objects` | medium (orphans found) / info | Content objects present but unreachable from /Root or /Info — typical of deleted content. Only content counts: streams, fonts, images and form XObjects, pages, annotations (`data.kinds`). A bare array, number or plain dictionary (macOS/iOS Quartz leaves its page box `[0 0 612 792]`), or an empty stream (`/Length` ≤ 16 or nothing once decoded — PDFium leaves an empty Form XObject per page) is not counted (`data.trivial_ignored`) |
| `javascript_or_openaction` | medium (flagged) / info | Embedded JS or auto-run action |
| `optional_content_groups` | low / info | Layers present |
| `editable_text_over_scan` | medium | Page structure (`services/forensics/page_structure.py`, from the drawing order): an image covering ≥ 90 % of the page drawn **before** visible live text (≥ 15 runs spanning ≥ 50 % of the page height) — a scan converted to editable text, **whatever the fonts are called** (a converter's `-NNNN` subsets, listed in `data.converter_font_pages`, or ordinary `ABCDEF+` subsets as Acrobat's editor leaves them). Does not depend on XMP, so it still holds when the metadata is stripped. An image drawn *over* the text (a flattened page pasted on top) is not this structure. A normal searchable scan keeps its OCR text invisible (render mode 3). Such pages also get the ghost-content check ([07a](07a-ghost-content.md)) |
| `producer_scrubbed` | medium | The XMP packet was written by Adobe's own library (`x:xmptk` "Adobe XMP Core …", read from the raw stream), yet Info Producer/Creator, XMP Producer/CreatorTool and the edit history are all empty. Adobe software always records its name and saves there, so they were removed afterwards. A fully empty file is `metadata_entirely_stripped` instead |
| `web_page_origin` | low / medium | The producer is a browser's or an HTML engine's PDF output (PDFium, Skia/PDF, Chrome, wkhtmltopdf, Puppeteer, Playwright, WeasyPrint, Prince…): the document was built as a web page and printed. **Medium** (`metadata.web_page_origin`, 10) when it also has no CreationDate or uses ≥ 3 stock colours of a CSS framework (Bootstrap 5, Tailwind — `services/forensics/pdf_facts.py`); **low** (shown only) with the engine alone, since school portals print from browsers too |
| `digital_signature` | info | Signature field present / signed / unsigned / absent |
| `info_dictionary`, `xmp_metadata`, `xmp_history`, `revision_count`, `document_id_chain` | info | Raw context |

The check `result` is **`flag` if any finding is `medium` or `high`**, else `pass`.

All `metadata.*` rules together add at most the company's metadata cap (default 40) to a case's score
— see [11](11-risk-scoring-engine.md).

## Output shape (real, truncated)

```jsonc
{
  "result": "flag",
  "details": [
    {"finding": "orphaned_objects", "severity": "medium",
     "description": "Found 25 object(s) present in the file but not reachable from /Root or /Info — likely left…",
     "data": {"count": 25, "object_numbers": [2, 3, "…"]}},
    {"finding": "info_dictionary", "severity": "info", "description": "Info dictionary has 6 field(s).",
     "data": {"/Creator": "Adobe Photoshop 2024 (Windows)", "/ModDate": "D:20260902164500Z",
              "/CreationDate": "D:20260801090000Z", "/Producer": "Adobe Photoshop 2024 (Windows)", "…": "…"}}
  ]
}
```

`details` is a **list** of findings (the same shape ELA, copy-move, duplicate and visual review use, so
one findings-list UI renders them all). These findings carry no `page`/`bounding_box`; metadata is not
positional.

## Known limitations

- **PDF only.** Images and Word files are not analyzed.
- Many legitimate tools leave incremental updates or editor fingerprints (a scanned-then-annotated
  invoice, for example). A finding is a reason to look, not proof.
- A determined forger can rewrite or strip metadata; `metadata_entirely_stripped` is itself scored
  because clean-room generators rarely strip everything.
- Uploads are PDF-only, so every new document is analysed; a non-PDF stored before that rule is a quiet
  no-op.
- The visual-review task cross-references this result (`metadata_forensics_correlation`) to strengthen
  or explain its own findings; that cross-reference is deliberately **not scored** to avoid double
  counting.

## Risk rules fed

| `rule_id` | Finding | Fires on | Weight | Severity |
|---|---|---|---:|---|
| `metadata.pdf_unreadable` | `pdf_unreadable` | any | 20 | high |
| `metadata.stripped` | `metadata_entirely_stripped` | any | 12 | medium |
| `metadata.editing_software_detected` | `editing_software_detected` | any | 30 | high |
| `metadata.creation_date_mismatch` | `info_xmp_creation_date_mismatch` | any | 18 | high |
| `metadata.mod_date_mismatch` | `info_xmp_mod_date_mismatch` | any | 18 | high |
| `metadata.producer_mismatch` | `info_xmp_producer_mismatch` | any | 10 | medium |
| `metadata.pdf_editor_producer` | `pdf_editor_detected` | any | 10 | medium |
| `metadata.modified_after_creation` | `mod_date_after_creation_date` | any | 15 | high |
| `metadata.incremental_update_single` | `incremental_updates_present` | medium | 8 | medium |
| `metadata.incremental_update_multiple` | `incremental_updates_present` | high | 20 | high |
| `metadata.history_tool_anomaly` | `history_editing_tool_anomaly` | any | 12 | medium |
| `metadata.history_scanned_doc_edited` | `history_scanned_document_edited` | any | 15 | high |
| `metadata.editable_text_over_scan` (v2: any font naming) | `editable_text_over_scan` | medium, high | 10 | medium |
| `metadata.producer_scrubbed` | `producer_scrubbed` | any | 10 | medium |
| `metadata.web_page_origin` | `web_page_origin` | medium, high | 10 | medium |
| `metadata.history_edit_after_final` | `history_edit_after_final_date` | any | 22 | high |
| `metadata.history_shorter_than_revisions` | `history_shorter_than_revisions` | any | 8 | medium |
| `metadata.history_absent_despite_revisions` | `history_absent_despite_revisions` | any | 8 | medium |
| `metadata.orphaned_objects` (v3: content only, empty streams ignored) | `orphaned_objects` | medium, high | 8 | medium |
| `metadata.javascript_or_openaction` | `javascript_or_openaction` | medium, high | 10 | medium |

Note the two severities: a rule's `severity` (how it is displayed) is independent of the *finding's*
severity (which the rule may filter on with `severity_in`).
