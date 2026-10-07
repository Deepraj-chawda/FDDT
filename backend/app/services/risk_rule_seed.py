"""
Initial `risk_rules` seed (SPECIFICATION.md section 3.3) — one rule per distinct
flaggable condition the built checks actually produce. Loaded by the
Alembic migration that adds the versioned rules table, and reused by
tests (`seed_risk_rules`).

These are STARTING VALUES. They are deliberately editable at runtime via
Settings > Risk Rules (app/api/settings.py) — an admin/client tunes them,
each edit creating a new immutable version. Nothing in the scoring engine
reads a weight from Python.

HOW WEIGHTS WERE CHOSEN
Scores are summed and capped at 100; default tiers are low < 30, medium
30-59, high >= 60. So, roughly:
  - A single hard, hard-to-innocently-explain integrity signal (an
    editing tool in the PDF history, a cloned-region cluster, a
    near-duplicate of a previously submitted document, an amount that
    doesn't reconcile across documents) is weighted 30-40: on its own it
    reaches "medium", and paired with any second signal it reaches "high".
  - Corroborating forensic signals that innocent workflows also produce
    (incremental saves, ELA recompression regions) are 8-25: several
    together matter, one alone usually does not.
  - Probabilistic vision-model judgments (visual inconsistencies, "looks
    AI-generated") are deliberately low, 8-12. SPECIFICATION.md is explicit that
    these are best-effort signals, never a standalone verdict.
  - Weak data-plausibility signals (malformed reference number, issuer/
    date noise) are 6-15.
  - Missing-but-expected evidence (issuer not in registry, no signature
    where one is expected) is 10-15: a reason to look, not proof.
The metadata rules ("metadata.*") together add at most the company's
metadata score cap (risk_settings.metadata_score_cap, default 40): one edit
leaves several metadata traces, which must not max the score on their own.
Two signals here are also intentionally NOT scored: `low`-severity
cross-document findings (the expected claim-vs-evidence issuer/date noise
— see app/services/cross_document_service.py) and `info` findings, which
are data points rather than flags.

CONDITION SHAPES (evaluated by app/services/risk_scoring_service.py)
  {"match": "finding", "check_type": ..., "finding": name | "finding_in": [...],
   "severity_in": [...]}                     a finding inside a check's `details` list
  {"match": "sub_check", "check_type": "field_validation", "sub_check": name}
                                             a field_validation sub-check with status "flag"
  {"match": "check_result", "check_type": ..., "result": "flag"}
                                             a check whose top-level result is that value
  {"match": "cross_document", "field_name": ..., "severity_in": [...]}
                                             a cross_document_findings row
  {"match": "signature_match", "result": ...}
                                             a signature_matches row (advisory comparison)

REASON TEMPLATES may use: {document} {count} {page} {description}
{reason} {issuer} {person} {field}. Unknown/missing placeholders render
as an empty string rather than raising.
"""
from __future__ import annotations

from typing import Any

FORENSICS = "forensics"
CONSISTENCY = "consistency"
VERIFICATION = "verification"
DUPLICATION = "duplication"


def _finding(check_type: str, **kw: Any) -> dict[str, Any]:
    return {"match": "finding", "check_type": check_type, **kw}


SEED_RULES: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- metadata
    {
        "rule_id": "metadata.pdf_unreadable",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="pdf_unreadable"),
        "weight": 20,
        "severity": "high",
        "reason_template": "'{document}' could not be parsed as a valid PDF — its internal structure is damaged or malformed.",
    },
    {
        "rule_id": "metadata.stripped",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="metadata_entirely_stripped"),
        # Honest scanner/print-to-PDF output is often metadata-light, so this
        # is weighted below the other metadata signals.
        "weight": 12,
        "severity": "medium",
        "reason_template": "'{document}' carries no metadata at all — it may have been scrubbed to hide its origin.",
    },
    {
        "rule_id": "metadata.editing_software_detected",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="editing_software_detected"),
        # Image/PDF editors in the toolchain of a supposedly original
        # business document is the most direct manipulation indicator.
        "weight": 30,
        "severity": "high",
        "reason_template": "'{document}': editing software was found in its metadata. {description}",
    },
    {
        "rule_id": "metadata.pdf_editor_producer",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="pdf_editor_detected"),
        # An online/desktop PDF editor (iLovePDF, Smallpdf, Sejda, ...) last
        # wrote the file. Also used innocently (compress, merge, sign), so
        # far below an image editor.
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}' was last saved by a PDF editor, not a scanner or billing system. {description}",
    },
    {
        "rule_id": "metadata.creation_date_mismatch",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="info_xmp_creation_date_mismatch"),
        "weight": 18,
        "severity": "high",
        "reason_template": "'{document}': the creation dates recorded in its two metadata blocks disagree.",
    },
    {
        "rule_id": "metadata.mod_date_mismatch",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="info_xmp_mod_date_mismatch"),
        "weight": 18,
        "severity": "high",
        "reason_template": "'{document}': the modification dates recorded in its two metadata blocks disagree.",
    },
    {
        "rule_id": "metadata.producer_mismatch",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="info_xmp_producer_mismatch"),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': the producing software named in its two metadata blocks differs.",
    },
    {
        "rule_id": "metadata.modified_after_creation",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="mod_date_after_creation_date"),
        "weight": 15,
        "severity": "high",
        "reason_template": "'{document}' was modified after it was created. {description}",
    },
    {
        "rule_id": "metadata.incremental_update_single",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="incremental_updates_present", severity_in=["medium"]),
        "weight": 8,
        "severity": "medium",
        "reason_template": "'{document}' was saved incrementally after its original version — content may have been appended or changed.",
    },
    {
        "rule_id": "metadata.incremental_update_multiple",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="incremental_updates_present", severity_in=["high"]),
        "weight": 20,
        "severity": "high",
        "reason_template": "'{document}' has multiple incremental saves after its original version — repeated post-creation edits.",
    },
    {
        "rule_id": "metadata.history_tool_anomaly",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="history_editing_tool_anomaly"),
        "weight": 12,
        "severity": "medium",
        "reason_template": "'{document}': its edit history names a tool that doesn't fit its stated origin.",
    },
    {
        "rule_id": "metadata.history_scanned_doc_edited",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        # Acrobat recorded "editedScannedDoc": the scan was converted to
        # editable text and edited — the document's own history says so.
        "condition": _finding("metadata_forensics", finding="history_scanned_document_edited"),
        "weight": 15,
        "severity": "high",
        "reason_template": "'{document}': its edit history records that the scanned page was converted to editable text and edited. {description}",
    },
    {
        "rule_id": "metadata.editable_text_over_scan",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        # Page structure, not metadata: a scan whose text was re-created as
        # visible live text over it. Still there when the XMP history is
        # stripped. v2: whatever the fonts are called (a converter's "-NNNN"
        # subsets, or ordinary "ABCDEF+" subsets as Acrobat's editor leaves).
        "condition": _finding("metadata_forensics", finding="editable_text_over_scan", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}' is a scan converted to editable text — its words and numbers can be retyped or deleted like a word-processor file. {description}",
    },
    {
        "rule_id": "metadata.producer_scrubbed",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        # Adobe's XMP library wrote the metadata, yet the tool names and the
        # edit history are gone: removed to hide which tool last saved it.
        "condition": _finding("metadata_forensics", finding="producer_scrubbed"),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': the names of the tools that wrote it were removed from its metadata. {description}",
    },
    {
        "rule_id": "metadata.history_edit_after_final",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="history_edit_after_final_date"),
        "weight": 22,
        "severity": "high",
        "reason_template": "'{document}': its edit history records a change after the document's final date.",
    },
    {
        "rule_id": "metadata.history_shorter_than_revisions",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="history_shorter_than_revisions"),
        "weight": 8,
        "severity": "medium",
        "reason_template": "'{document}': its recorded edit history is shorter than its number of revisions.",
    },
    {
        "rule_id": "metadata.history_absent_despite_revisions",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="history_absent_despite_revisions"),
        "weight": 8,
        "severity": "medium",
        "reason_template": "'{document}' has multiple revisions but no edit history — the history may have been removed.",
    },
    {
        "rule_id": "metadata.orphaned_objects",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        "condition": _finding("metadata_forensics", finding="orphaned_objects", severity_in=["medium", "high"]),
        "weight": 8,
        "severity": "medium",
        "reason_template": "'{document}' contains orphaned internal objects left behind by editing.",
    },
    {
        "rule_id": "metadata.javascript_or_openaction",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        # v2: the finding is only raised for executable/outward-reaching
        # content (JavaScript, Launch, URI, SubmitForm, ImportData, GoToR/E),
        # not for an /OpenAction that only sets the initial view.
        "condition": _finding("metadata_forensics", finding="javascript_or_openaction", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}' embeds JavaScript or an action that runs code or reaches outside the document — unusual for a business document. {description}",
    },
    # -------------------------------------------------------- image tampering
    {
        "rule_id": "ela.tamper_region_detected",
        "category": FORENSICS,
        "check_type": "error_level_analysis",
        "condition": _finding("error_level_analysis", finding="recompression_error_region"),
        # ELA is a hint, not a verdict: weak against print-and-rescan and
        # prone to false positives on recompressed scans (SPECIFICATION.md 3.2).
        "weight": 25,
        "severity": "medium",
        "reason_template": "'{document}': {count} localized area(s) show higher recompression error than the rest of the page (error level analysis) — a possible sign of editing. First on page {page}.",
    },
    {
        "rule_id": "ela.anti_forensic_signal",
        "category": FORENSICS,
        "check_type": "error_level_analysis",
        "condition": _finding("error_level_analysis", finding="anti_forensic_signal"),
        # Split from the region flag on purpose: an apparent attempt to
        # erase ELA's own evidence is a stronger signal than the evidence.
        "weight": 30,
        "severity": "high",
        "reason_template": "'{document}': page {page} shows signs of smoothing/anti-forensic processing that could hide edit traces.",
    },
    {
        "rule_id": "copy_move.cluster_detected",
        "category": FORENSICS,
        "check_type": "copy_move_detection",
        "condition": _finding("copy_move_detection", finding="copy_move_cluster"),
        "weight": 35,
        "severity": "high",
        "reason_template": "'{document}': {count} region(s) were copied and pasted within the same page (copy-move detection), first on page {page}.",
    },
    # ------------------------------------------------------ font consistency
    {
        "rule_id": "font.inconsistency",
        "category": FORENSICS,
        "check_type": "font_consistency",
        # "high" findings come from the PDF's text layer: the font names
        # themselves, not a judgment — a hard signal, weighted like copy-move.
        # v2: on a scan converted to editable text, only a full font among the
        # converter's -NNNN subset fonts counts (two subsets side by side do
        # not); a number whose characters mix sizes counts too.
        # v3: text in a second embedded copy (subset) of a font the page
        # already embeds, beside text in the main copy, counts too.
        "condition": {
            "match": "finding",
            "check_type": "font_consistency",
            "finding_in": ["font_inconsistency", "font_size_inconsistency", "font_subset_split"],
            "severity_in": ["high"],
        },
        "weight": 40,
        "severity": "high",
        "reason_template": "'{document}': {count} piece(s) of text are set in a different font, at a different size, or in a second copy of the same font, from the text around them — a sign they were typed in after the document was produced. {description}",
    },
    {
        "rule_id": "font.inconsistency_scanned",
        "category": FORENSICS,
        "check_type": "font_consistency",
        # "medium" findings come from OCR font recognition on a scanned page:
        # an estimate, corroborated across several words, so weighted lower.
        # v2: once 3+ amounts on a page are scored, the page's other amounts
        # are re-checked more leniently (app/services/forensics/font_consistency.py).
        "condition": _finding("font_consistency", finding="font_inconsistency", severity_in=["medium"]),
        "weight": 25,
        "severity": "medium",
        "reason_template": "'{document}': on the scanned page, {count} word(s) look like a different font from the text around them (OCR font recognition — an estimate). {description}",
    },
    # ------------------------------------------ deleted / replaced content (ghost text)
    {
        "rule_id": "content.deleted_ghost_block",
        "category": FORENSICS,
        "check_type": "ghost_content",
        # On a scan converted to editable text: faint traces of erased text
        # in the scan with no live text on top — content deleted after the
        # conversion (app/services/forensics/ghost_content.py).
        "condition": _finding("ghost_content", finding="ghost_deleted_block", severity_in=["high"]),
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {count} block(s) of text were deleted after the scan was converted to editable text — their faint traces remain in the scanned background. {description}",
    },
    {
        "rule_id": "content.replaced_ghost_line",
        "category": FORENSICS,
        "check_type": "ghost_content",
        # The trace of a line in the scan runs on well past the live text on
        # top: the line was shortened or rewritten.
        "condition": _finding("ghost_content", finding="ghost_replaced_line", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': {count} line(s) were shortened or rewritten after the scan was converted to editable text — the trace of the original runs on past the text. {description}",
    },
    # -------------------------------------------- visual review (vision model)
    {
        "rule_id": "visual.font_inconsistency",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        "condition": _finding("visual_inconsistency_review", finding="visual_font_consistency", severity_in=["medium", "high"]),
        "weight": 12,
        "severity": "medium",
        "reason_template": "'{document}': the vision-model review reports inconsistent fonts. {description}",
    },
    {
        "rule_id": "visual.alignment_inconsistency",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        # v2: findings about a signature/stamp, or matching the scan's skew, are not counted
        # (apply_region_filters in app/services/visual_inconsistency_service.py).
        "condition": _finding("visual_inconsistency_review", finding="visual_text_alignment", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': the vision-model review reports misaligned text. {description}",
    },
    {
        "rule_id": "visual.color_contrast_inconsistency",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        "condition": _finding("visual_inconsistency_review", finding="visual_color_contrast_consistency", severity_in=["medium", "high"]),
        "weight": 12,
        "severity": "medium",
        "reason_template": "'{document}': the vision-model review reports a patch with a different background/contrast. {description}",
    },
    {
        "rule_id": "visual.sharpness_inconsistency",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        # v2: findings about a signature/stamp are not counted
        # (apply_region_filters in app/services/visual_inconsistency_service.py).
        "condition": _finding("visual_inconsistency_review", finding="visual_resolution_sharpness_consistency", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': the vision-model review reports a region with different sharpness. {description}",
    },
    {
        "rule_id": "visual.lighting_inconsistency",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        "condition": _finding("visual_inconsistency_review", finding="visual_shadow_lighting_consistency", severity_in=["medium", "high"]),
        "weight": 8,
        "severity": "low",
        "reason_template": "'{document}': the vision-model review reports inconsistent lighting/shadows. {description}",
    },
    {
        "rule_id": "ai.generated_content_suspected",
        "category": FORENSICS,
        "check_type": "visual_inconsistency_review",
        "condition": _finding("visual_inconsistency_review", finding="ai_generation_assessment", severity_in=["medium", "high"]),
        # Experimental, unproven on scanned business documents (SPECIFICATION.md
        # 3.2/4): the lowest weight in the set, never a standalone verdict.
        # It fires per document, and vision models over-report it on clean
        # synthetic/scanned documents, so 2 documents at 6 each must stay
        # well inside "low" even alongside one other weak signal.
        "weight": 6,
        "severity": "low",
        "reason_template": "'{document}': the vision-model review reports possible signs of AI-generated content (experimental, probabilistic signal).",
    },
    # ------------------------------------------------------------- duplication
    {
        "rule_id": "duplicate.cross_case_match",
        "category": DUPLICATION,
        "check_type": "duplicate_detection",
        # v2: a page match whose invoice number, date, amount or student
        # differ is "same_template_page" (shown, not scored), not this.
        "condition": _finding("duplicate_detection", finding="near_duplicate_page"),
        "weight": 40,
        "severity": "high",
        "reason_template": "'{document}': {count} page(s) are near-identical to previously submitted documents. {description}",
    },
    # ------------------------------------------------- field-level consistency
    {
        "rule_id": "field.date_in_future",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "date_in_future"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.subtotal_line_item_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # v2: lines are summed to the cent (one cent per summed line), lines
        # marked PAID on the document are never summed, and the reason names
        # the lines summed and left out.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "subtotal_line_item_consistency"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.total_tax_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "total_tax_consistency"},
        "weight": 20,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.line_item_arithmetic_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # A printed line total that is not quantity x unit price: almost never
        # an innocent typo on a system-generated document.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "line_item_arithmetic"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.tax_rate_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # v2: passes when the tax is the rate applied to any subset of the
        # payable line items (mixed-rate / partly zero-rated invoices).
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "tax_rate_consistency"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.amount_in_words_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # The figures were changed but the amount in words was not (or vice
        # versa) — a classic sign of an edited total.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "amount_in_words_consistency"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.iban_trn_validation",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # A printed IBAN with a bad checksum/length, a bank code naming a
        # different bank, or an account number it doesn't contain; or a UAE
        # TRN in the wrong format. Nothing on a pass.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "iban_trn_validation"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "metadata.rescan_conflict",
        "category": FORENSICS,
        "check_type": "field_validation",
        # Phone-scanner watermark on an office-MFP PDF: printed and rescanned,
        # which hides pre-print edits from pixel forensics. Not proof of an
        # edit on its own, so weighted like other corroborating signals.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "rescan_conflict"},
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "metadata.document_date_after_file_creation",
        "category": FORENSICS,
        "check_type": "field_validation",
        # The printed date is after the PDF file's own creation, and the file
        # was modified on or after that date: an older file re-dated. Files
        # generated in advance and never touched again pass.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "document_date_vs_file_creation"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.installment_sum_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # Installment rows (Term I/II/III, Installment 1/2) that nearly, but
        # not exactly, add up to the line they split: one part was changed.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "installment_consistency"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.date_sequence_inconsistent",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # Dated after it was printed, or printed after its PDF was made.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "date_sequence"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.fake_scan_watermark",
        "category": FORENSICS,
        "check_type": "field_validation",
        # A scanner app's watermark on a file with no image: typed in to look like a scan.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "fake_scan_watermark"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "signature.synthetic_stamp",
        "category": VERIFICATION,
        "check_type": "field_validation",
        # A stamp that is live text / vector lines in the file, not an image of an ink stamp.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "stamp_authenticity"},
        "weight": 25,
        "severity": "high",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "signature.synthetic_stamp_unsigned",
        "category": VERIFICATION,
        "check_type": "field_validation",
        # A typed-in stamp and no signature anywhere.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "synthetic_stamp_unsigned"},
        "weight": 5,
        "severity": "low",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.tax_invoice_without_trn",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # Titled TAX INVOICE but no UAE TRN on it.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "tax_invoice_trn"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.multiple_invoices_same_period",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # One file holding two invoices for the same month.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "multiple_invoices"},
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.per_invoice_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        # An invoice of a multi-invoice file whose own arithmetic does not hold.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "per_invoice_checks"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "metadata.web_page_origin",
        "category": FORENSICS,
        "check_type": "metadata_forensics",
        # Printed from a browser / HTML engine AND built like a web page (no
        # creation date, a CSS framework's stock colours). The engine alone
        # is low and not scored: school portals print from browsers too.
        "condition": _finding("metadata_forensics", finding="web_page_origin", severity_in=["medium", "high"]),
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}' was built as a web page and printed to PDF. {description}",
    },
    {
        "rule_id": "field.period_quantity_mismatch",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "period_quantity_consistency"},
        "weight": 10,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "field.reference_number_malformed",
        "category": CONSISTENCY,
        "check_type": "field_validation",
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "reference_number_format"},
        "weight": 6,
        "severity": "low",
        "reason_template": "'{document}': {reason}",
    },
    # ------------------------------------------------------ cross-document
    {
        "rule_id": "cross_doc.amount_mismatch",
        "category": CONSISTENCY,
        "check_type": "cross_document_consistency",
        "condition": {"match": "cross_document", "field_name": "amount", "severity_in": ["medium", "high"]},
        # The reconciliation signal this whole check exists for.
        "weight": 35,
        "severity": "high",
        "reason_template": "Amounts do not reconcile across this case's documents. {description}",
    },
    {
        "rule_id": "cross_doc.date_mismatch",
        "category": CONSISTENCY,
        "check_type": "cross_document_consistency",
        "condition": {"match": "cross_document", "field_name": "date", "severity_in": ["medium", "high"]},
        "weight": 12,
        "severity": "medium",
        "reason_template": "Dates differ across this case's documents. {description}",
    },
    {
        "rule_id": "cross_doc.issuer_mismatch",
        "category": CONSISTENCY,
        "check_type": "cross_document_consistency",
        "condition": {"match": "cross_document", "field_name": "issuer", "severity_in": ["medium", "high"]},
        "weight": 15,
        "severity": "medium",
        "reason_template": "Issuer names differ across this case's documents. {description}",
    },
    # ---------------------------------------------------------- verification
    {
        "rule_id": "issuer.not_in_registry",
        "category": VERIFICATION,
        "check_type": "issuer_verification",
        "condition": {"match": "check_result", "check_type": "issuer_verification", "result": "flag"},
        # v2: the check reports "not_checked" (and this does not fire) when
        # the registry has no active entries for the document's country or
        # kind of issuer; OCR noise in the name is normalized before matching.
        # Fires on ANY document whose issuer isn't in the registry — including
        # evidence documents from third parties (e.g. a payment processor) —
        # so on its own it must stay below "medium" (30).
        "weight": 15,
        "severity": "medium",
        "reason_template": "Issuer '{issuer}' on '{document}' did not match any known issuer in the registry.",
    },
    {
        "rule_id": "signature.stamp_issuer_mismatch",
        "category": VERIFICATION,
        "check_type": "field_validation",
        # The stamp's text (read by signature/stamp detection) names another
        # organisation than the issuer. A vision-model reading, so weighted as
        # a reason to look rather than proof.
        "condition": {"match": "sub_check", "check_type": "field_validation", "sub_check": "stamp_issuer_consistency"},
        "weight": 15,
        "severity": "medium",
        "reason_template": "'{document}': {reason}",
    },
    {
        "rule_id": "signature.expected_missing",
        "category": VERIFICATION,
        "check_type": "signature_stamp_detection",
        "condition": {"match": "check_result", "check_type": "signature_stamp_detection", "result": "flag"},
        "weight": 10,
        "severity": "low",
        "reason_template": "No signature or stamp was located on '{document}', though this kind of document normally carries one.",
    },
    {
        "rule_id": "signature.inconsistent_with_reference",
        "category": VERIFICATION,
        "check_type": "signature_comparison",
        "condition": {"match": "signature_match", "result": "inconsistent"},
        # Advisory only (SPECIFICATION.md 2.3): a qualitative visual comparison, so
        # weighted as a strong reason to look, not a verdict.
        "weight": 25,
        "severity": "medium",
        "reason_template": "The signature/stamp on '{document}' looks visually inconsistent with the reference on file for {person}. {reason}",
    },
    {
        "rule_id": "signature.possibly_consistent_only",
        "category": VERIFICATION,
        "check_type": "signature_comparison",
        "condition": {"match": "signature_match", "result": "possibly_consistent"},
        "weight": 5,
        "severity": "low",
        "reason_template": "The signature/stamp on '{document}' is only possibly consistent with the reference on file for {person}.",
    },
    {
        "rule_id": "signature.identical_reuse",
        "category": VERIFICATION,
        "check_type": "signature_comparison",
        "condition": {"match": "signature_match", "result": "identical_reuse"},
        # A classical pixel comparison, not a model opinion. Weighted modestly on
        # its own because a stored e-signature image is legitimately reused.
        "weight": 15,
        "severity": "medium",
        "reason_template": "The signature on '{document}' is pixel-identical to the reference on file for {person}; a handwritten signature never repeats exactly, so the same image file appears to have been reused. {reason}",
    },
    {
        "rule_id": "signature.reused_different_signer",
        "category": VERIFICATION,
        "check_type": "signature_comparison",
        "condition": {"match": "signature_match", "result": "reused_different_signer"},
        "weight": 35,
        "severity": "high",
        "reason_template": "The signature on '{document}' is pixel-identical to the reference on file for {person}, yet the printed signer beneath it differs. {reason}",
    },
]


# The version of each built-in rule's definition, where it is past 1. Bumped
# when what a rule matches changes meaning (its check was changed), together
# with a migration that gives every company a new version row of that rule
# (app/db/migrations/versions/a9d3f6b2c8e4_check_accuracy_fixes.py) — never by
# editing old rows, so assessments made before keep their own versions.
SEED_RULE_VERSIONS: dict[str, int] = {
    "metadata.javascript_or_openaction": 2,
    "field.subtotal_line_item_mismatch": 3,
    "field.tax_rate_mismatch": 3,
    "font.inconsistency_scanned": 2,
    "visual.alignment_inconsistency": 2,
    "visual.sharpness_inconsistency": 2,
    "issuer.not_in_registry": 2,
    "font.inconsistency": 3,
    "metadata.editable_text_over_scan": 2,
    "metadata.orphaned_objects": 3,
    "signature.stamp_issuer_mismatch": 2,
    "duplicate.cross_case_match": 2,
}


def ensure_rule_templates(db) -> int:
    """Make sure the platform rule template (risk_rule_templates) exists. On
    a fresh install it is filled from SEED_RULES; on a migrated database the
    multi-tenancy hardening migration filled it from the Default Company's
    current rules, and it is never touched again here. Returns rows inserted.
    Platform session only (the company app role has no access to the table)."""
    from sqlalchemy import func, select

    from app.models.risk_rule_template import RiskRuleTemplate

    if db.execute(select(func.count()).select_from(RiskRuleTemplate)).scalar_one():
        return 0
    for spec in SEED_RULES:
        db.add(RiskRuleTemplate(is_active=True, **spec))
    db.flush()
    return len(SEED_RULES)


def seed_risk_rules(db, company_id) -> int:
    """Give one company its starting rule set: a copy (version 1, company_id
    stamped) of every ACTIVE platform rule template the company doesn't have
    yet, plus default tier thresholds. Idempotent. Returns how many rule rows
    were inserted.

    Called when a company is created (app/api/platform.py). The copy is the
    company's own from then on — editing a template later never changes an
    existing company's rules, only what companies created afterwards start
    with (no retroactive change, same principle as rule versioning)."""
    from sqlalchemy import select

    from app.models.risk_rule import RiskRule
    from app.models.risk_rule_template import RiskRuleTemplate
    from app.models.risk_setting import RiskSetting

    ensure_rule_templates(db)
    templates = db.execute(
        select(RiskRuleTemplate).where(RiskRuleTemplate.is_active.is_(True)).order_by(RiskRuleTemplate.rule_id)
    ).scalars().all()
    existing = set(
        db.execute(select(RiskRule.rule_id).where(RiskRule.company_id == company_id)).scalars().all()
    )
    inserted = 0
    for t in templates:
        if t.rule_id in existing:
            continue
        db.add(
            RiskRule(
                company_id=company_id,
                rule_id=t.rule_id,
                category=t.category,
                check_type=t.check_type,
                condition=dict(t.condition),
                weight=t.weight,
                severity=t.severity,
                reason_template=t.reason_template,
                is_active=True,
                version=1,
                change_note="Default rule set (copied from the platform rule template)",
            )
        )
        inserted += 1
    if db.execute(select(RiskSetting.id).where(RiskSetting.company_id == company_id)).first() is None:
        db.add(RiskSetting(company_id=company_id))
    db.flush()
    return inserted
