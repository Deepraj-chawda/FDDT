"""
What an admin can build a NEW risk rule out of (Settings > Risk Rules > Add
rule) — the structured, validated counterpart of the raw `condition` JSON
that app/services/risk_rule_seed.py documents.

A rule's `condition` is what app/services/risk_scoring_service.py evaluates,
so it must be one of the shapes that engine understands and must point at
something the checks can actually produce; a typo'd finding name would
create a rule that silently never fires. So the form is built from this
catalog (served by GET /settings/risk-rule-options) and the API re-validates
against it — the admin picks from real values, and never writes JSON.

Adding a rule never changes any case that was already scored: assessments
freeze the rule versions that produced them, and only cases scored after the
rule exists can trigger it.
"""
from __future__ import annotations

from typing import Any

from app.models.document_check import DocumentCheckType

CATEGORIES = ["forensics", "consistency", "verification", "duplication"]

# check_type -> finding names its `details` list can contain (the checks whose
# details are a list of findings; see each service under app/services/).
FINDING_NAMES: dict[str, list[str]] = {
    "metadata_forensics": [
        "editing_software_detected", "mod_date_after_creation_date", "metadata_entirely_stripped",
        "incremental_updates_present", "history_absent_despite_revisions", "history_edit_after_final_date",
        "history_editing_tool_anomaly", "history_shorter_than_revisions", "info_xmp_creation_date_mismatch",
        "info_xmp_mod_date_mismatch", "info_xmp_producer_mismatch", "javascript_or_openaction",
        "optional_content_groups", "orphaned_objects", "pdf_unreadable", "digital_signature",
        "document_id_chain", "info_dictionary", "xmp_metadata", "xmp_history", "revision_count",
        "pdf_editor_detected", "history_scanned_document_edited", "editable_text_over_scan", "producer_scrubbed", "web_page_origin",
    ],
    "font_consistency": ["font_inconsistency", "font_size_inconsistency", "font_subset_split", "font_family_inventory"],
    "ghost_content": [
        "ghost_deleted_block", "ghost_replaced_line", "ghost_show_through", "ghost_near_signature",
        "ghost_content_scope",
    ],
    "error_level_analysis": ["recompression_error_region", "anti_forensic_signal", "pixel_analysis_limited", "ela_scope"],
    "copy_move_detection": ["copy_move_cluster", "pixel_analysis_limited"],
    "duplicate_detection": ["near_duplicate_page", "same_template_page"],
    "visual_inconsistency_review": [
        "visual_font_consistency", "visual_text_alignment", "visual_color_contrast_consistency",
        "visual_resolution_sharpness_consistency", "visual_shadow_lighting_consistency",
        "ai_generation_assessment",
    ],
}

# Checks whose overall result (pass/flag) can itself be a rule.
CHECK_RESULT_TYPES = [
    DocumentCheckType.metadata_forensics.value,
    DocumentCheckType.error_level_analysis.value,
    DocumentCheckType.copy_move_detection.value,
    DocumentCheckType.duplicate_detection.value,
    DocumentCheckType.visual_inconsistency_review.value,
    DocumentCheckType.font_consistency.value,
    DocumentCheckType.ghost_content.value,
    DocumentCheckType.signature_stamp_detection.value,
    DocumentCheckType.field_validation.value,
    DocumentCheckType.issuer_verification.value,
]

SUB_CHECKS = [
    "date_in_future", "line_item_arithmetic", "subtotal_line_item_consistency", "total_tax_consistency",
    "tax_rate_consistency", "amount_in_words_consistency", "reference_number_format",
    "iban_trn_validation", "rescan_conflict", "period_quantity_consistency", "document_date_vs_file_creation",
    "stamp_issuer_consistency", "installment_consistency", "date_sequence", "stamp_authenticity",
    "synthetic_stamp_unsigned", "fake_scan_watermark", "tax_invoice_trn", "multiple_invoices", "per_invoice_checks",
]
CROSS_FIELDS = ["amount", "date", "issuer"]
# "consistent" is the good outcome; a rule that fires on it would be a mistake.
SIGNATURE_RESULTS = [
    "inconsistent", "possibly_consistent", "cannot_determine", "identical_reuse", "reused_different_signer",
]

MATCH_KINDS = [
    ("finding", "A specific finding from a check", "e.g. ELA reports a recompression region"),
    ("check_result", "A check's overall result is Flag", "fires once per document where the check flagged"),
    ("sub_check", "A field-validation rule failed", "e.g. the total does not match subtotal + tax"),
    ("cross_document", "Two documents disagree on a field", "issuer, date or amount, optionally by severity"),
    ("signature_match", "A signature comparison verdict", "advisory visual comparison against a reference"),
]

PLACEHOLDERS = ["document", "count", "page", "description", "reason", "issuer", "person", "field"]


def build_condition(
    match: str,
    *,
    check_type: str | None = None,
    finding: str | None = None,
    severity_in: list[str] | None = None,
    sub_check: str | None = None,
    field_name: str | None = None,
    signature_result: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """(`risk_rules.check_type`, `condition`) for a structured rule choice.
    Raises ValueError with an admin-readable message if the choice isn't
    something the engine can evaluate."""
    sev = sorted(set(severity_in or []))
    if match == "finding":
        if check_type not in FINDING_NAMES:
            raise ValueError("Choose which check produces the finding.")
        if finding not in FINDING_NAMES[check_type]:
            raise ValueError(f"'{finding}' is not a finding that {check_type} can produce.")
        condition: dict[str, Any] = {"match": "finding", "check_type": check_type, "finding": finding}
        if sev:
            condition["severity_in"] = sev
        return check_type, condition
    if match == "check_result":
        if check_type not in CHECK_RESULT_TYPES:
            raise ValueError("Choose a check whose result can be used.")
        return check_type, {"match": "check_result", "check_type": check_type, "result": "flag"}
    if match == "sub_check":
        if sub_check not in SUB_CHECKS:
            raise ValueError("Choose which field-validation rule to match.")
        return "field_validation", {"match": "sub_check", "check_type": "field_validation", "sub_check": sub_check}
    if match == "cross_document":
        if field_name not in CROSS_FIELDS:
            raise ValueError("Choose which field the documents disagree on.")
        condition = {"match": "cross_document", "field_name": field_name}
        if sev:
            condition["severity_in"] = sev
        return "cross_document_consistency", condition
    if match == "signature_match":
        if signature_result not in SIGNATURE_RESULTS:
            raise ValueError("Choose which signature verdict to match.")
        return "signature_comparison", {"match": "signature_match", "result": signature_result}
    raise ValueError("Unknown rule type.")
