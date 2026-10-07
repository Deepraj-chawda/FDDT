"""
Data-gathering half of the per-case PDF report (see app/services/
case_report_pdf.py for the rendering half, and case_report_service.py for
the orchestration/storage).

Everything the report says is READ from what the pipeline already stored —
`document_checks`, `case_risk_assessments`, `cross_document_findings`,
`case_actions`, `signature_matches` and the append-only `audit_log`. Nothing
here re-runs a check, re-scores a case, regenerates a risk reason (the
`reason` text is the one frozen in `case_risk_assessments.triggered_reasons`
at scoring time) or writes its own audit trail.

ORIGINALS ARE ONLY READ. Each original file is downloaded from Blob Storage
purely so it can (a) be re-hashed, to prove the report's chain-of-custody
hash still matches the stored bytes, and (b) be rendered to page images for
the annotated-evidence pages. Nothing is ever written back to the original
blob (SPECIFICATION.md section 4: original files are stored immutably).
"""
from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.services.check_summaries import (
    STAMP_SUB_CHECKS, risk_reason_short, sub_check_item, summarize_document_checks,
)
from app.models.case import Case, CaseStatus
from app.models.case_action import CaseAction, CaseActionType
from app.models.cross_document_finding import CrossDocumentFinding
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.signature_match import SignatureMatch
from app.models.signature_reference import SignatureReference
from app.models.user import User, role_label
from app.schemas.signature import SIGNATURE_MATCH_RESULT_LABELS
from app.core.config import APP_FULL_NAME, APP_NAME, settings
from app.models.issuer_registry import IssuerRegistry
from app.services.field_exception_service import (
    EXCEPTION_SEVERITIES,
    cross_document_regions,
    with_field_regions,
)
from app.services.case_link_service import find_linked_cases
from app.services.field_regions import field_label, format_field_value, humanize, valid_box
from app.services.risk_scoring_service import latest_assessment, pipeline_status
from app.services.storage_service import StorageService

DASH = "—"

# How each localized check is drawn — mirrors the LIVE Case Detail overlay
# convention (frontend/src/components/case/PdfOverlayViewer.tsx +
# DocumentChecksPanel.tsx's OVERLAY_COLOR_BY_CHECK_TYPE), deliberately not
# a new one: ELA = solid red, copy-move = solid orange, model-judgment
# checks (visual inconsistency review, signature comparison) = dashed and
# labelled "approximate".
#   Field exception (rule-based) -> solid purple box: deterministic comparisons
#   of extracted values, so neither pixel forensics (red/orange) nor model
#   judgment (dashed).
#   Font mismatch (font consistency check) -> solid fuchsia box around the
#   text set in an out-of-place font.
#   Ghost content (deleted / replaced content check) -> solid teal box around
#   the faint trace of erased text, shown with the enhanced crop of it.
AnnotationKind = Literal["ela", "copy_move", "model", "field", "font", "ghost", "page_level"]

_MATERIAL_SEVERITIES = ("medium", "high")
# Notes derived from other checks / explanatory notices — never findings in
# their own right (same list the risk engine leaves unscored).
_NOTE_FINDINGS = {
    "metadata_forensics_correlation", "experimental_signal_notice", "pixel_analysis_limited",
    "ghost_content_scope", "ghost_show_through", "ghost_near_signature",
}

# Ordered as they appear in each document's checks-summary table:
# (check type, label, what it looks at).
CHECK_CATALOG: list[tuple[DocumentCheckType, str, str]] = [
    (
        DocumentCheckType.metadata_forensics,
        "Metadata forensics",
        "PDF metadata, edit history and incremental-save markers.",
    ),
    (
        DocumentCheckType.font_consistency,
        "Font consistency",
        "Text set in a different font family from the text around it (text layer, or OCR font recognition on scans).",
    ),
    (
        DocumentCheckType.error_level_analysis,
        "Error level analysis (ELA)",
        "Pixel-level recompression differences that can indicate edited regions.",
    ),
    (
        DocumentCheckType.copy_move_detection,
        "Copy-move detection",
        "Regions of a page that were duplicated elsewhere on the same page.",
    ),
    (
        DocumentCheckType.ghost_content,
        "Deleted / replaced content (ghost text)",
        "On a scan converted to editable text: faint traces of the original text left in the scanned background, "
        "with no live text on top (deleted) or running on past it (shortened).",
    ),
    (
        DocumentCheckType.visual_inconsistency_review,
        "Visual inconsistency / AI-generation review",
        "Model review of fonts, alignment, contrast, sharpness, lighting and signs of AI generation.",
    ),
    (
        DocumentCheckType.signature_stamp_detection,
        "Signature / stamp detection",
        "Whether a signature or stamp mark is present where this kind of document normally carries one.",
    ),
    (
        DocumentCheckType.duplicate_detection,
        "Duplicate detection",
        "Perceptual-hash comparison of each page against previously submitted pages.",
    ),
    (
        DocumentCheckType.field_validation,
        "Field validation",
        "Date, subtotal / tax / total and reference-number plausibility.",
    ),
    (
        DocumentCheckType.issuer_verification,
        "Issuer verification",
        "Issuer name matched against the issuer registry.",
    ),
]
CROSS_DOCUMENT_LABEL = "Cross-document consistency"
CROSS_DOCUMENT_DESC = "Issuer, date and amount compared pairwise across every document in the case."
REQUIRED_CORE_FIELDS = ("issuer", "reference_number", "date", "amount")
SIGNATURE_COMPARISON_LABEL = "Signature comparison (advisory)"
SIGNATURE_COMPARISON_DESC = (
    "Qualitative visual comparison against a reviewer-set reference signature."
)
CROSS_DOCUMENT_FIELDS = ("issuer", "date", "amount")

_VISUAL_CATEGORY_LABELS = {
    "font_consistency": "font consistency",
    "text_alignment": "text alignment",
    "color_contrast_consistency": "color / contrast consistency",
    "resolution_sharpness_consistency": "resolution / sharpness consistency",
    "shadow_lighting_consistency": "shadow / lighting consistency",
}

_EVENT_LABELS = {
    "case_created": "Case created",
    "document_uploaded": "Document uploaded",
    "document_processing_completed": "Text extraction, classification and field extraction completed",
    "document_processing_failed": "Text extraction / classification failed",
    "document_checks_completed": "Field validation and issuer verification completed",
    "metadata_forensics_completed": "Metadata forensics completed",
    "tampering_checks_completed": "ELA, copy-move and ghost-content checks completed",
    "tampering_checks_failed": "ELA and copy-move checks failed",
    "duplicate_check_completed": "Duplicate detection completed",
    "visual_inconsistency_review_completed": "Visual inconsistency review completed",
    "font_consistency_completed": "Font consistency check completed",
    "font_consistency_failed": "Font consistency check failed",
    "signature_stamp_detection_completed": "Signature / stamp detection completed",
    "signature_reference_created": "Signature reference set by reviewer",
    "signature_comparison_completed": "Signature comparison completed",
    "cross_document_check_completed": "Cross-document consistency check completed",
    "risk_assessment_completed": "Risk score computed",
    "case_status_changed": "Case status changed",
    "case_approved": "Reviewer decision: approved",
    "case_rejected": "Reviewer decision: rejected",
    "case_escalated": "Case escalated to L2",
    "case_report_generated": "Case report generated",
}
# Events cut from the excerpt only if there are more than this many.
AUDIT_EXCERPT_LIMIT = 150


# ---------------------------------------------------------------------------
# Report data model (plain data — the PDF module has no DB/ORM access)
# ---------------------------------------------------------------------------

@dataclass
class PageAnnotation:
    page: int  # 1-based
    kind: AnnotationKind
    check_label: str
    caption: str  # short text drawn next to the box
    description: str  # full text listed under the rendered page
    # Normalized (0-1) x, y, width, height of the box, or None for a
    # page-level finding that the check did not localize.
    box: tuple[float, float, float, float] | None = None
    # document_checks.check_type value, or "signature_comparison" /
    # "cross_document_consistency" for the two that aren't a check row.
    check_type: str = ""
    # Short, plain-language exception line ("ELA: possible tamper region").
    exception_text: str = ""
    # An image shown with the highlight (the ghost-content check's enhanced
    # crop of the faint trace), PNG bytes.
    image_png: bytes | None = None
    # "R1", "R2", ... — assigned once, report-wide, after every annotation
    # exists (assign_region_ids); the Exceptions section and Section 9 both
    # refer to a highlight by this ID.
    region_id: str = ""


@dataclass
class ExceptionItem:
    """One thing that did NOT pass, as a concrete item for the Exceptions
    section. `annotations` are the highlight regions that show it (their
    region IDs are read at render time, after IDs are assigned)."""

    source: str  # the check that produced it
    text: str
    detail: str = ""
    document: str | None = None
    document_index: int | None = None
    page: int | None = None
    severity: str | None = None
    annotations: list[PageAnnotation] = field(default_factory=list)
    # ids of the documents this exception involves (two for a cross-document one)
    involved: list[str] = field(default_factory=list)


@dataclass
class FindingRow:
    """One triggered risk reason (Explainable Findings)."""

    severity: str
    weight: float
    check: str
    document: str | None
    page: int | None
    text: str  # the stored, already-rendered reason
    annotations: list[PageAnnotation] = field(default_factory=list)
    # Short form, shown first (app/services/check_summaries.py).
    title: str = ""
    short: str = ""


@dataclass
class TechnicalBlock:
    title: str
    rows: list[tuple[str, str]]


@dataclass
class CheckRow:
    name: str
    description: str
    result: str  # pass | flag | review | limited | not_applicable | not_checked | failed | in_progress
    summary: str  # the headline
    # One short line per problem ("Amounts retyped — 1,450.00 · ..."), then notes.
    lines: list[str] = field(default_factory=list)


@dataclass
class FieldRow:
    name: str
    value: str
    confidence: float | None
    uncertain: bool
    page: int | None = None  # page the value was located on, if a position was recorded


@dataclass
class HashVerification:
    recorded_sha256: str
    # "match" | "mismatch" | "unverified" (original couldn't be fetched)
    status: str
    computed_sha256: str | None = None
    note: str | None = None


@dataclass
class DocumentSection:
    document_id: uuid.UUID
    filename: str
    document_type: str
    uploaded_at: datetime
    hash: HashVerification
    processing_note: str | None
    check_rows: list[CheckRow]
    annotations: list[PageAnnotation]
    fields: list[FieldRow]
    # The original's bytes — kept only when there are annotations to draw
    # (so pages without findings are never rendered), and only ever read.
    original_bytes: bytes | None = None
    original_kind: str = "pdf"  # pymupdf filetype hint for the original
    index: int = 0  # 1-based position in the case, for "Document 2 page 1"
    exceptions: list[ExceptionItem] = field(default_factory=list)
    file_size_bytes: int | None = None
    content_type: str | None = None


@dataclass
class ReasonRow:
    severity: str
    weight: float
    text: str
    document: str | None


@dataclass
class RiskSummary:
    tier: str
    score: int
    raw_score: float
    computed_at: datetime
    assessment_id: uuid.UUID
    reasons: list[ReasonRow]
    rule_versions: list[tuple[str, int, float]]  # (rule_id, version, weight)
    thresholds: dict[str, int]
    # Rule families capped together when scored: {"metadata": {"cap", "points"}}.
    group_caps: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class DecisionInfo:
    label: str  # "Approved" | "Rejected" | "Auto-approved"
    by: str | None
    at: datetime | None
    note: str | None


@dataclass
class EscalationInfo:
    by: str | None
    at: datetime
    note: str | None


@dataclass
class CrossRow:
    field: str
    documents: str
    result: str  # consistent | mismatch | not_compared
    detail: str


@dataclass
class AuditRow:
    at: datetime
    event: str
    actor: str
    detail: str


@dataclass
class CoverageRow:
    name: str
    status: str  # ran | not_run | not_applicable
    detail: str


@dataclass
class LinkedCaseRow:
    case_number: str
    reasons: list[str]


@dataclass
class ReportData:
    report_id: uuid.UUID
    generated_at: datetime
    generated_by_name: str
    generated_by_role: str
    case_id: uuid.UUID
    case_number: str
    case_type: str
    case_status: str
    submitter: str
    submitted_at: datetime
    document_types: list[str]
    documents: list[DocumentSection]
    risk: RiskSummary | None
    pipeline_pending: list[str]
    decision: DecisionInfo | None
    escalation: EscalationInfo | None
    cross_document: list[CrossRow] | None  # None => case has < 2 documents
    audit: list[AuditRow]
    audit_truncated: int
    coverage: list[CoverageRow] = field(default_factory=list)
    app_name: str = APP_NAME
    app_full_name: str = APP_FULL_NAME
    exceptions: list[ExceptionItem] = field(default_factory=list)
    findings: list[FindingRow] = field(default_factory=list)
    technical: list[TechnicalBlock] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    # Other cases sharing a scanner, parent or family/student ID — a reviewer
    # note, never scored (app/services/case_link_service.py).
    linked_cases: list[LinkedCaseRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _user_name(user: User | None) -> str:
    if user is None:
        return "System"
    return user.full_name or user.email


def _actor_label(user: User | None, role_at_time: str | None) -> str:
    """"Name (Reviewer L1)". Uses the role recorded when the action happened
    (so a later promotion doesn't rewrite history), falling back to the
    user's current role for rows recorded before roles were captured."""
    if user is None:
        return "System"
    return f"{_user_name(user)} ({role_label(role_at_time or user.role)})"


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _details(check: DocumentCheck | None) -> Any:
    if check is None or not isinstance(check.result, dict):
        return None
    return check.result.get("details")


def _result(check: DocumentCheck | None) -> str | None:
    if check is None or not isinstance(check.result, dict):
        return None
    value = check.result.get("result")
    return value if isinstance(value, str) else None


def _valid_box(raw: Any) -> tuple[int, tuple[float, float, float, float]] | None:
    """(page, (x, y, w, h)) from a stored `bounding_box` dict, or None if it
    is missing/degenerate. Boxes are normalized 0-1 page fractions."""
    box = valid_box(raw)
    if box is None:
        return None
    return box["page"], (box["x"], box["y"], box["width"], box["height"])


# ---------------------------------------------------------------------------
# Findings -> highlight annotations + exceptions
# ---------------------------------------------------------------------------

def _is_material(item: dict[str, Any]) -> bool:
    return item.get("finding") not in _NOTE_FINDINGS and (
        item.get("severity") in _MATERIAL_SEVERITIES or _valid_box(item.get("bounding_box")) is not None
    )


def _describe_finding(
    check_type: DocumentCheckType, item: dict[str, Any], located: bool
) -> tuple[AnnotationKind, str, str] | None:
    """(kind, drawn caption, plain exception line) for one localized-check
    finding, or None for a check type this doesn't draw."""
    name = str(item.get("finding") or "")
    if check_type == DocumentCheckType.font_consistency:
        if name == "font_subset_split":
            return (
                "font" if located else "page_level",
                f"Font mismatch {DASH} second embedded copy of the font",
                "Font consistency: text set in a second embedded copy of a font the page already uses",
            )
        scanned = (item.get("data") or {}).get("source") == "ocr"
        return (
            "font" if located else "page_level",
            f"Font mismatch {DASH} text set in a different font family" + (" (scan, estimated)" if scanned else ""),
            "Font consistency: text set in a different font family from the text around it"
            + (" (OCR font recognition on a scan)" if scanned else ""),
        )
    if check_type == DocumentCheckType.ghost_content:
        if name == "ghost_replaced_line":
            return (
                "ghost" if located else "page_level",
                f"Ghost text {DASH} line shortened or rewritten",
                "Deleted / replaced content: a line was shortened or rewritten after the scan was converted",
            )
        return (
            "ghost" if located else "page_level",
            f"Ghost text {DASH} deleted content",
            "Deleted / replaced content: text deleted after the scan was converted to editable text",
        )
    if check_type == DocumentCheckType.error_level_analysis:
        if located:
            return "ela", f"ELA {DASH} possible tamper region", "ELA: possible tamper region"
        return (
            "page_level", f"ELA {DASH} possible anti-forensic signal", "ELA: possible anti-forensic signal"
        )
    if check_type == DocumentCheckType.copy_move_detection:
        return (
            "copy_move" if located else "page_level",
            f"Copy-move {DASH} possible duplicated region",
            "Copy-move: a region appears duplicated elsewhere on the page",
        )
    if check_type == DocumentCheckType.visual_inconsistency_review:
        kind: AnnotationKind = "model" if located else "page_level"
        if name == "ai_generation_assessment":
            return (
                kind,
                f"AI-generation assessment {DASH} possible synthetic content (approximate)",
                "AI-generation assessment: possible synthetic content (model judgment)",
            )
        category = _VISUAL_CATEGORY_LABELS.get(
            str((item.get("data") or {}).get("category") or name.removeprefix("visual_")),
            "visual inconsistency",
        )
        return (
            kind,
            f"Visual review {DASH} {category} (approximate)",
            f"Visual review: {category} inconsistency (model judgment)",
        )
    if check_type == DocumentCheckType.duplicate_detection:
        return (
            "page_level",
            f"Duplicate detection {DASH} near-duplicate page",
            "Duplicate: page is a near-duplicate of an earlier submission",
        )
    return None


def _crop_png(item: dict[str, Any]) -> bytes | None:
    """The enhanced crop a ghost-content finding carries, decoded."""
    data = item.get("data")
    encoded = data.get("crop_png_base64") if isinstance(data, dict) else None
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError):
        return None


def derive_findings(
    doc: Document,
    index: int,
    latest_checks: dict[DocumentCheckType, DocumentCheck],
    signature_matches: list[tuple[SignatureMatch, str]],
) -> tuple[list[PageAnnotation], list[ExceptionItem]]:
    """Everything on this document that did NOT pass, as (a) highlight
    annotations to draw on rendered pages and (b) concrete exception items.

    An annotation is a finding with a bounding box (the live UI draws every
    one of those) or a medium/high finding attributed to a page but not
    localized (page render + a page-level note instead of a box). Info-level
    notes never qualify. The same pass builds the exception items, so each
    exception knows which highlight regions show it.
    """
    annotations: list[PageAnnotation] = []
    exceptions: list[ExceptionItem] = []

    def add_exception(**kw: Any) -> ExceptionItem:
        item = ExceptionItem(
            document=doc.original_filename, document_index=index, involved=[str(doc.id)], **kw
        )
        exceptions.append(item)
        return item

    summaries = summarize_document_checks(list(latest_checks.values()), members=True)
    for check_type, label, _ in CHECK_CATALOG:
        check = latest_checks.get(check_type)
        if check is None:
            continue
        summary = summaries.get(check_type.value) or {"items": []}
        if check.status == DocumentCheckStatus.failed:
            add_exception(
                source=label, text=f"{label}: the check failed to run",
                detail=_shorten(check.error_message or "", 240),
            )
            continue
        if check.status != DocumentCheckStatus.completed:
            continue
        details = _details(check)

        # -- list-shaped findings (ELA, copy-move, visual, duplicate, metadata):
        # every located finding is drawn; the exceptions are the summary's
        # items (one per problem, grouping findings with one cause).
        if isinstance(details, list):
            items = summary["items"]
            shown: dict[int, list[PageAnnotation]] = {}
            for item in details:
                if not isinstance(item, dict) or not _is_material(item):
                    continue
                located = _valid_box(item.get("bounding_box"))
                page = located[0] if located else item.get("page")
                description = str(item.get("description") or item.get("finding") or "")
                described = _describe_finding(check_type, item, located is not None)
                annotation: PageAnnotation | None = None
                if described and isinstance(page, int) and page >= 1:
                    kind, caption, text = described
                    annotation = PageAnnotation(
                        page=page, kind=kind, check_label=label, caption=caption, description=description,
                        box=located[1] if located else None, check_type=check_type.value, exception_text=text,
                        image_png=_crop_png(item),
                    )
                    annotations.append(annotation)
                plain = " ".join(description.split())
                owner = next(
                    (n for n, it in enumerate(items) if item.get("finding") in it["findings"] and plain in it.get("members", [])),
                    None,
                )
                if owner is not None:
                    shown.setdefault(owner, [])
                    if annotation is not None:
                        shown[owner].append(annotation)
                    continue
                if item.get("severity") == "info":
                    continue  # drawn for context; an info note is not an exception
                add_exception(
                    source=label,
                    text=described[2] if described else f"{label}: {_shorten(description, 260)}",
                    # (when there is no separate plain-language line, the text already IS the description)
                    detail=_shorten(description, 320) if described else "",
                    page=page if isinstance(page, int) else None,
                    severity=item.get("severity"),
                    annotations=[annotation] if annotation else [],
                )
            for n, linked in shown.items():
                it = items[n]
                line = f"{it['title']} — {it['text']}"
                for annotation in linked:
                    annotation.exception_text = line
                add_exception(
                    source=label, text=line, detail=it["detail"],
                    page=it.get("page") if isinstance(it.get("page"), int) else (linked[0].page if linked else None),
                    severity=it["severity"], annotations=linked,
                )
            continue

        result = _result(check)

        # -- field validation: one exception per flagged sub-check, drawn on the fields
        if check_type == DocumentCheckType.field_validation and isinstance(details, dict):
            enriched = with_field_regions(check.result, doc.extracted_fields)
            for name, sub in ((enriched or {}).get("details") or {}).items():
                if not isinstance(sub, dict) or sub.get("status") != "flag":
                    continue
                reason = str(sub.get("reason") or name)
                short = sub_check_item(name, sub)
                title = f"{short['title']} — {short['text']}"
                linked: list[PageAnnotation] = []
                for region in sub.get("regions") or []:
                    box = _valid_box(region.get("bounding_box"))
                    if box is None:
                        continue
                    linked.append(
                        PageAnnotation(
                            page=box[0], kind="field", check_label=label,
                            caption=str(region.get("caption") or ""),
                            description=f"{reason} ({region.get('label')}: {region.get('value')})",
                            box=box[1], check_type=check_type.value, exception_text=title,
                        )
                    )
                annotations.extend(linked)
                add_exception(
                    source=label, text=title, detail=reason,
                    page=linked[0].page if linked else None, severity="medium", annotations=linked,
                )
            continue

        if result != "flag":
            continue
        _, result_summary = _summarize_check(check_type, check)
        shorts = [
            it for it in summary["items"]
            if not (check_type == DocumentCheckType.signature_stamp_detection and set(STAMP_SUB_CHECKS) & set(it["findings"]))
        ]
        if shorts:
            for it in shorts:
                add_exception(
                    source=label, text=f"{it['title']} — {it['text']}", detail=it["detail"], severity=it["severity"]
                )
        else:
            add_exception(source=label, text=f"{label}: {result_summary}")

    # -- required fields that were never extracted (nothing to draw)
    if doc.processing_status == DocumentProcessingStatus.complete and isinstance(doc.extracted_fields, dict):
        core = doc.extracted_fields.get("core_fields") or {}
        for name in REQUIRED_CORE_FIELDS:
            if (core.get(name) or {}).get("value") in (None, ""):
                add_exception(
                    source="Field extraction",
                    text=f"Missing required field: {field_label(name)} was not found on this document",
                    severity="medium",
                )

    # -- signature comparison: a model judgment, drawn (dashed, "approximate")
    # on this document's located signature region unless the verdict is "consistent".
    detection = latest_checks.get(DocumentCheckType.signature_stamp_detection)
    detection_details = _details(detection)
    sig_box = (
        _valid_box(detection_details.get("bounding_box")) if isinstance(detection_details, dict) else None
    )
    for match, person_name in signature_matches:
        verdict = match.result.value
        if verdict == "consistent":
            continue
        short = {
            "possibly_consistent": "possibly consistent, not clearly matching",
            "inconsistent": "inconsistent with the reference",
            "cannot_determine": "cannot determine",
            "identical_reuse": "pixel-identical to the reference, image appears reused",
            "reused_different_signer": "pixel-identical to the reference under a different printed signer",
        }.get(verdict, verdict)
        description = (
            f"Compared to the reference signature of '{person_name}': "
            f"{SIGNATURE_MATCH_RESULT_LABELS.get(verdict, verdict)}. {match.reasoning or ''}"
        ).strip()
        annotation = None
        if sig_box is not None:
            annotation = PageAnnotation(
                page=sig_box[0], kind="model", check_label=SIGNATURE_COMPARISON_LABEL,
                caption=f"Signature comparison {DASH} {short.split(',')[0]} (approximate)",
                description=description, box=sig_box[1], check_type="signature_comparison",
                exception_text=f"Signature comparison: {short}",
            )
            annotations.append(annotation)
        add_exception(
            source=SIGNATURE_COMPARISON_LABEL, text=f"Signature comparison: {short}",
            detail=_shorten(description, 320), page=sig_box[0] if sig_box else None,
            severity="medium", annotations=[annotation] if annotation else [],
        )
    return annotations, exceptions


def assign_region_ids(sections: list["DocumentSection"]) -> list[PageAnnotation]:
    """Numbers every highlight R1, R2, ... in document order, then page order
    (so Section 9 reads top to bottom), and returns them in that order."""
    ordered: list[PageAnnotation] = []
    for section in sections:
        section.annotations.sort(key=lambda a: a.page)  # stable: keeps check order within a page
        ordered.extend(section.annotations)
    for number, annotation in enumerate(ordered, 1):
        annotation.region_id = f"R{number}"
    return ordered


# ---------------------------------------------------------------------------
# Checks summary table
# ---------------------------------------------------------------------------

def _summarize_check(check_type: DocumentCheckType, check: DocumentCheck) -> tuple[str, str]:
    """(result, one-line summary) for one stored check row."""
    if check.status == DocumentCheckStatus.failed:
        return "failed", _shorten(check.error_message or "The check failed to run.", 200)
    if check.status != DocumentCheckStatus.completed:
        return "in_progress", "Still running when this report was generated."

    result = _result(check) or "pass"
    details = _details(check)
    if result == "not_applicable":
        return "not_applicable", "Not applicable to this document."
    if result == "limited" and isinstance(details, list):
        note = next((d for d in details if isinstance(d, dict) and d.get("finding") == "pixel_analysis_limited"), {})
        return "limited", _shorten(str(note.get("description") or "Limited: this check could not see the page's text."), 260)

    if check_type == DocumentCheckType.issuer_verification and isinstance(details, dict):
        name = details.get("issuer_name")
        matched = details.get("matched_registry_name")
        if result == "not_checked":
            return "not_checked", _shorten(str(details.get("reason") or "Not checked."), 220)
        if result == "flag":
            return "flag", _shorten(
                f"Issuer '{name}' did not match any known issuer in the registry." if name
                else "Issuer did not match any known issuer in the registry.",
                220,
            )
        if matched:
            return "pass", _shorten(
                f"Matched registry entry '{matched}' (similarity {details.get('match_score')}).", 220
            )
        return "pass", "Issuer matched the registry."

    if check_type == DocumentCheckType.ghost_content and isinstance(details, list):
        found = [d for d in details if isinstance(d, dict)]
        deleted = [d for d in found if d.get("finding") == "ghost_deleted_block"]
        replaced = [d for d in found if d.get("finding") == "ghost_replaced_line"]
        scope = next((d for d in found if d.get("finding") == "ghost_content_scope"), {})
        pages = ", ".join(str(p.get("page")) for p in (scope.get("data") or {}).get("pages") or [])
        if deleted or replaced:
            parts = []
            if deleted:
                lines = sum(int((d.get("data") or {}).get("lines") or 0) for d in deleted)
                parts.append(f"{len(deleted)} deleted text block(s) ({lines} line(s))")
            if replaced:
                texts = ", ".join(repr(str((d.get("data") or {}).get("text") or "")) for d in replaced[:3])
                parts.append(f"{len(replaced)} shortened or rewritten line(s) ({texts})")
            return "flag", _shorten(
                "Faint traces of the original text in the scanned background show " + " and ".join(parts) + ".", 260
            )
        return "pass", f"Converted page(s) {pages} searched: no traces of deleted or shortened text."

    if check_type == DocumentCheckType.signature_stamp_detection and isinstance(details, dict):
        detected = details.get("detected") or []
        if result == "flag":
            return "flag", "A signature or stamp would be expected on this document but none was located."
        return "pass", f"{len(detected)} signature/stamp region(s) located (presence only)."

    if isinstance(details, dict):  # field_validation: {sub_check: {status, reason}}
        flagged = [
            sub.get("reason", name)
            for name, sub in details.items()
            if isinstance(sub, dict) and sub.get("status") == "flag"
        ]
        if flagged:
            return "flag", _shorten(" ".join(str(r) for r in flagged), 260)
        passed = sum(1 for s in details.values() if isinstance(s, dict) and s.get("status") == "pass")
        return "pass", f"{passed} sub-check(s) passed; none flagged."

    if isinstance(details, list):
        material = [
            d for d in details
            if isinstance(d, dict)
            and d.get("finding") not in _NOTE_FINDINGS
            and (d.get("severity") in _MATERIAL_SEVERITIES or (d.get("bounding_box") and d.get("severity") != "info"))
        ]
        scored = [d for d in material if d.get("severity") in _MATERIAL_SEVERITIES]
        if material and not scored and result != "flag":
            first = material[0]
            return "review", _shorten(
                f"{len(material)} finding(s) shown for review, not scored. {first.get('description') or ''}", 260
            )
        if result == "flag" or material:
            first = material[0] if material else (details[0] if details else {})
            desc = str(first.get("description") or first.get("finding") or "Flagged.") if isinstance(first, dict) else "Flagged."
            count = len(material) or 1
            return "flag", _shorten(f"{count} finding(s). {desc}", 260)
        return "pass", "No anomaly detected by this check."

    return ("flag" if result == "flag" else "pass"), (
        "Flagged." if result == "flag" else "No anomaly detected by this check."
    )


def _check_rows(
    latest_checks: dict[DocumentCheckType, DocumentCheck],
    signature_matches: list[tuple[SignatureMatch, str]],
) -> list[CheckRow]:
    rows: list[CheckRow] = []
    summaries = summarize_document_checks(list(latest_checks.values()))
    for check_type, label, description in CHECK_CATALOG:
        check = latest_checks.get(check_type)
        if check is None:
            continue  # didn't run on this document — reported in "coverage" instead
        result, _ = _summarize_check(check_type, check)
        short = summaries.get(check_type.value) or {"headline": "", "items": [], "notes": []}
        rows.append(
            CheckRow(
                name=label, description=description, result=result, summary=short["headline"],
                lines=[f"{it['title']} — {it['text']}" for it in short["items"]] + list(short["notes"]),
            )
        )
        if check_type == DocumentCheckType.signature_stamp_detection and signature_matches:
            verdicts = [m.result.value for m, _ in signature_matches]
            outcome = (
                "flag" if "inconsistent" in verdicts
                else "review" if any(v != "consistent" for v in verdicts)
                else "pass"
            )
            lines = [
                f"vs. reference '{name}': {SIGNATURE_MATCH_RESULT_LABELS.get(m.result.value, m.result.value)}"
                for m, name in signature_matches
            ]
            rows.append(
                CheckRow(
                    name=SIGNATURE_COMPARISON_LABEL,
                    description=SIGNATURE_COMPARISON_DESC,
                    result=outcome,
                    summary=_shorten("; ".join(lines), 260),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Extracted fields
# ---------------------------------------------------------------------------

_CORE_FIELD_ORDER = ("issuer", "reference_number", "date", "amount", "subtotal", "tax_rate", "tax_amount")


def _format_value(value: Any, currency: Any = None) -> str:
    if value is None or value == "":
        return "Not found"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        text = f"{value:,.2f}"
        return f"{text} {currency}" if currency else text
    return str(value)


def extracted_field_rows(extracted: dict[str, Any] | None) -> list[FieldRow]:
    if not isinstance(extracted, dict):
        return []
    rows: list[FieldRow] = []
    core = extracted.get("core_fields") or {}
    ordered = [n for n in _CORE_FIELD_ORDER if n in core] + [n for n in core if n not in _CORE_FIELD_ORDER]
    for name in ordered:
        item = core.get(name)
        if not isinstance(item, dict):
            continue
        rows.append(
            FieldRow(
                name=humanize(name),
                value=_format_value(item.get("value"), item.get("currency")),
                confidence=item.get("confidence"),
                uncertain=bool(item.get("uncertain")),
                page=(valid_box(item.get("bounding_box")) or {}).get("page"),
            )
        )
    for item in extracted.get("additional_fields") or []:
        if isinstance(item, dict):
            rows.append(
                FieldRow(
                    name=humanize(str(item.get("field_name") or "field")),
                    value=_format_value(item.get("value")),
                    confidence=item.get("confidence"),
                    uncertain=bool(item.get("uncertain")),
                    page=(valid_box(item.get("bounding_box")) or {}).get("page"),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Cross-document consistency
# ---------------------------------------------------------------------------

def cross_document_rows(
    documents: list[Document], findings: list[CrossDocumentFinding]
) -> list[CrossRow]:
    """Which shared fields were compared across documents, and the outcome,
    per document pair. Mismatches come straight from the stored
    `cross_document_findings` rows (the check is not re-run); a pair with no
    stored finding is "consistent" only if both documents actually have a
    value for the field, otherwise it's reported as "not compared"."""
    from itertools import combinations

    mismatches: dict[tuple[str, frozenset[str]], CrossDocumentFinding] = {}
    for finding in findings:
        ids = frozenset(str(i) for i in (finding.document_ids or []))
        mismatches[(finding.field_name, ids)] = finding

    def value_of(doc: Document, field_name: str) -> Any:
        core = (doc.extracted_fields or {}).get("core_fields") or {}
        return (core.get(field_name) or {}).get("value")

    rows: list[CrossRow] = []
    for doc_a, doc_b in combinations(documents, 2):
        pair = f"{doc_a.original_filename}  vs  {doc_b.original_filename}"
        for field_name in CROSS_DOCUMENT_FIELDS:
            finding = mismatches.get((field_name, frozenset((str(doc_a.id), str(doc_b.id)))))
            if finding is not None:
                rows.append(
                    CrossRow(
                        field=humanize(field_name),
                        documents=pair,
                        result="mismatch",
                        detail=f"{finding.description} (severity: {finding.severity.value})",
                    )
                )
            elif value_of(doc_a, field_name) is not None and value_of(doc_b, field_name) is not None:
                rows.append(
                    CrossRow(
                        field=humanize(field_name),
                        documents=pair,
                        result="consistent",
                        detail=(
                            f"Both documents show {_format_value(value_of(doc_a, field_name))}."
                            if _format_value(value_of(doc_a, field_name)) == _format_value(value_of(doc_b, field_name))
                            else "The values agree within the comparison tolerance."
                        ),
                    )
                )
            else:
                rows.append(
                    CrossRow(
                        field=humanize(field_name),
                        documents=pair,
                        result="not_compared",
                        detail="Not compared — the field was not extracted from one or both documents.",
                    )
                )
    return rows


# ---------------------------------------------------------------------------
# Audit trail excerpt
# ---------------------------------------------------------------------------

def _audit_detail(entry: AuditLog, filename: str | None) -> str:
    data = entry.event_data if isinstance(entry.event_data, dict) else {}
    parts: list[str] = []
    if filename:
        parts.append(filename)
    et = entry.event_type
    if et == "document_uploaded" and data.get("file_hash"):
        parts.append(f"SHA-256 {str(data['file_hash'])[:16]}…")
    elif et == "risk_assessment_completed":
        parts.append(f"tier {data.get('tier')}, score {data.get('score')}")
    elif et == "case_status_changed":
        parts.append(f"{data.get('from_status')} → {data.get('to_status')}")
        if data.get("reason"):
            parts.append(str(data["reason"]))
    elif et in ("case_approved", "case_rejected", "case_escalated"):
        note = data.get("reason") or data.get("note")
        if note:
            parts.append(f"“{note}”")
    return _shorten("; ".join(p for p in parts if p), 240)


def audit_rows(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> tuple[list[AuditRow], int]:
    entries = list(
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.case_id == case_id,
                AuditLog.company_id == company_id,
                AuditLog.event_type != "platform_admin_access",  # platform-only, never in a company report
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars().all()
    )
    actor_ids = {e.actor_user_id for e in entries if e.actor_user_id}
    actors = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(actor_ids))).scalars().all()}
        if actor_ids else {}
    )
    filenames = {
        d.id: d.original_filename
        for d in db.execute(select(Document).where(Document.case_id == case_id)).scalars().all()
    }
    truncated = max(0, len(entries) - AUDIT_EXCERPT_LIMIT)
    rows = [
        AuditRow(
            at=_aware(e.created_at),
            event=_EVENT_LABELS.get(e.event_type, humanize(e.event_type)),
            actor=_actor_label(
                actors.get(e.actor_user_id),
                (e.event_data or {}).get("actor_role") if isinstance(e.event_data, dict) else None,
            ) if e.actor_user_id else "System",
            detail=_audit_detail(e, filenames.get(e.document_id)),
        )
        for e in entries[:AUDIT_EXCERPT_LIMIT]
    ]
    return rows, truncated


# ---------------------------------------------------------------------------
# Coverage (Limitations section: "which checks ran on THIS case")
# ---------------------------------------------------------------------------

_NOT_APPLICABLE_WHY = {
    DocumentCheckType.ghost_content: " (no page is a scan converted to editable text)",
}


def _not_applicable_reason(check: DocumentCheck) -> str | None:
    """The short reason a check stored for being not applicable, if any."""
    details = _details(check)
    if isinstance(details, list):
        return next(
            (str((d.get("data") or {}).get("reason")) for d in details
             if isinstance(d, dict) and isinstance(d.get("data"), dict) and d["data"].get("reason")),
            None,
        )
    return None


def coverage_rows(
    documents: list[Document],
    latest_by_doc: dict[uuid.UUID, dict[DocumentCheckType, DocumentCheck]],
    *,
    has_signature_reference: bool,
    signature_comparisons: int,
) -> list[CoverageRow]:
    total = len(documents)
    rows: list[CoverageRow] = []
    for check_type, label, _ in CHECK_CATALOG:
        ran = [latest_by_doc[d.id][check_type] for d in documents if check_type in latest_by_doc[d.id]]
        completed = [c for c in ran if c.status == DocumentCheckStatus.completed]
        outcomes = [_summarize_check(check_type, c)[0] for c in completed]
        flagged = [o for o in outcomes if o == "flag"]
        not_applicable = [o for o in outcomes if o == "not_applicable"]
        failed = [c for c in ran if c.status == DocumentCheckStatus.failed]
        if not ran:
            rows.append(CoverageRow(label, "not_run", f"Did not run on any of the {total} document(s)."))
            continue
        if len(not_applicable) == len(ran):
            reasons = list(dict.fromkeys(r for c in completed if (r := _not_applicable_reason(c))))
            why = ("; ".join(reasons) if reasons else _NOT_APPLICABLE_WHY.get(check_type, "").strip(" ()")) or "—"
            rows.append(CoverageRow(label, "not_applicable", f"Not applicable: {why}."))
            continue
        detail = f"Ran on {len(ran)} of {total} document(s); {len(flagged)} flagged"
        if not_applicable:
            detail += f"; not applicable to {len(not_applicable)}"
        if failed:
            detail += f"; {len(failed)} failed to run"
        rows.append(CoverageRow(label, "ran", detail + "."))

    if signature_comparisons:
        rows.append(
            CoverageRow(
                SIGNATURE_COMPARISON_LABEL, "ran",
                f"{signature_comparisons} comparison(s) against a reviewer-set reference signature.",
            )
        )
    else:
        rows.append(
            CoverageRow(
                SIGNATURE_COMPARISON_LABEL,
                "not_run",
                "Did not run — "
                + (
                    "a reference signature was set but no other document had a comparable signature region."
                    if has_signature_reference
                    else "no reference signature was set for this case."
                ),
            )
        )
    if total >= 2:
        rows.append(
            CoverageRow(
                "Cross-document consistency", "ran",
                f"Issuer, date and amount compared across {total} documents.",
            )
        )
    else:
        rows.append(
            CoverageRow(
                "Cross-document consistency", "not_applicable",
                "Not applicable — this case has a single document.",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Technical parameters (Appendix) — read from each check's own constants so
# the report can never drift from what actually ran.
# ---------------------------------------------------------------------------

def technical_blocks(ran: set[DocumentCheckType], *, multi_document: bool, fields_located: bool) -> list[TechnicalBlock]:
    # Imported here: these pull in OpenCV/numpy, which the report only needs
    # for this appendix.
    from app.services import cross_document_service, signature_detection_service, visual_inconsistency_service
    from app.services.forensics import copy_move, ela
    from app.services.forensics.pdf_render import RENDER_DPI

    blocks: list[TechnicalBlock] = [
        TechnicalBlock(
            "Page rendering (input to ELA, copy-move, duplicate, visual review)",
            [("Render resolution", f"{RENDER_DPI} DPI"), ("Colour model", "RGB, converted to BGR for OpenCV")],
        )
    ]
    if DocumentCheckType.metadata_forensics in ran:
        blocks.append(TechnicalBlock("Metadata forensics", [
            ("Libraries", "pikepdf / PyMuPDF (PDF structure, XMP, incremental updates)"),
            ("Modified-after-created rule",
             f"flagged if ModDate is more than {settings.metadata_forensics_mod_date_threshold_seconds:g} second(s) after CreationDate"),
            ("Editing-software names matched", ", ".join(settings.metadata_forensics_editing_software_name_list)),
        ]))
    if DocumentCheckType.error_level_analysis in ran:
        blocks.append(TechnicalBlock("Error level analysis (ELA)", [
            ("JPEG recompression quality", str(ela.JPEG_RECOMPRESS_QUALITY)),
            ("Error gain (scale)", f"{ela._SCALE} (sqrt error term; the reference tool's default)"),
            ("Contrast stretch / LUT", "not applied — the raw error magnitude is thresholded directly"),
            ("Region threshold", f"page-relative percentile {ela._ERROR_PERCENTILE_THRESHOLD} "
                                 f"with an absolute floor of {ela._MIN_ABSOLUTE_ERROR_THRESHOLD}/255"),
            ("Region area accepted", f"{ela.MIN_REGION_AREA_FRACTION:.1%} – {ela.MAX_REGION_AREA_FRACTION:.0%} of the page"),
            ("Applies to", "pages with an embedded raster image only (born-digital text pages are exempt)"),
        ]))
    if DocumentCheckType.copy_move_detection in ran:
        blocks.append(TechnicalBlock("Copy-move detection", [
            ("Keypoint algorithm", f"{copy_move.DETECTOR_TYPE} (OpenCV), detector threshold {copy_move.BRISK_THRESHOLD}"),
            ("Response / matching / distance thresholds",
             f"{copy_move.RESPONSE_THRESHOLD} / {copy_move.MATCHING_THRESHOLD} / {copy_move.DISTANCE_THRESHOLD}"),
            ("Minimum cluster size", f"{copy_move.CLUSTER_SIZE} matching keypoint pairs"),
            ("Minimum region area", f"{copy_move.MIN_REGION_AREA_FRACTION:.0%} of the page"),
            ("Keypoint / match caps", f"{copy_move.MAX_FILTERED_KEYPOINTS:,} / {copy_move.MAX_MATCHES_TO_CLUSTER:,}"),
        ]))
    if DocumentCheckType.duplicate_detection in ran:
        blocks.append(TechnicalBlock("Duplicate detection", [
            ("Hash", "perceptual hash (imagehash pHash), 64-bit, one per rendered page"),
            ("Hamming-distance threshold", f"{settings.duplicate_hash_hamming_threshold} (≤ this is flagged)"),
            ("Compared against", "every page hash of every previously submitted document"),
        ]))
    if DocumentCheckType.visual_inconsistency_review in ran:
        blocks.append(TechnicalBlock("Visual inconsistency / AI-generation review", [
            ("Model", settings.azure_openai_deployment_name or "not configured (Azure OpenAI vision deployment)"),
            ("Independent runs per page", str(visual_inconsistency_service.RUNS_PER_PAGE)),
            ("A finding is kept if", "both runs report it, or one reports it with high confidence"),
            ("Image sent to model", f"page render, long edge ≤ {visual_inconsistency_service.MAX_IMAGE_DIMENSION}px"),
        ]))
    if DocumentCheckType.signature_stamp_detection in ran:
        blocks.append(TechnicalBlock("Signature / stamp detection and comparison", [
            ("Model", settings.azure_openai_deployment_name or "not configured (Azure OpenAI vision deployment)"),
            ("Pages analysed per document", f"first {signature_detection_service.MAX_PAGES_ANALYZED}"),
            ("Output", "presence and placement only; comparison is a qualitative verdict, never a numeric score"),
        ]))
    if DocumentCheckType.field_validation in ran:
        blocks.append(TechnicalBlock("Field validation", [
            ("Sub-checks", "date not in the future; subtotal vs line items; total vs subtotal + tax; reference-number format"),
            ("Amount tolerance", "1% of the amount, minimum 0.01"),
        ]))
    if DocumentCheckType.issuer_verification in ran:
        blocks.append(TechnicalBlock("Issuer verification", [
            ("Matching", "rapidfuzz weighted ratio against the active issuer registry, with a model-judged fallback for cross-script names"),
            ("Similarity threshold", f"{settings.issuer_fuzzy_match_threshold:g}"),
        ]))
    if multi_document:
        blocks.append(TechnicalBlock("Cross-document consistency", [
            ("Fields compared", "issuer, date, amount — every document pair"),
            ("Amount", "numeric, tolerance 1% (minimum 0.01); a currency difference is a mismatch"),
            ("Issuer", f"fuzzy similarity ≥ {cross_document_service._ISSUER_NAME_SIMILARITY_THRESHOLD:g}"),
            ("Date", "exact, after normalizing to ISO 8601"),
            ("Severity", "amount = high; date/issuer = medium, or low between a claim and its evidence document"),
        ]))
    if fields_located:
        blocks.append(TechnicalBlock("Field locations (purple highlights)", [
            ("Method", "extracted values matched back onto OCR word positions (Azure Document Intelligence Layout)"),
            ("Repeated values", "resolved by nearby keywords (“total”, “subtotal”, “VAT”, “date”…)"),
            ("Accuracy", "approximate; a field that cannot be matched is reported without a highlight"),
        ]))
    return blocks


# ---------------------------------------------------------------------------
# Assumptions (Limitations section) — specific to what actually ran
# ---------------------------------------------------------------------------

def build_assumptions(
    *,
    sections: list[DocumentSection],
    ran: set[DocumentCheckType],
    references: list[tuple[str, str]],
    active_issuers: int,
    generated_at: datetime,
    multi_document: bool,
    any_field_region: bool,
    non_pdf: int,
) -> list[str]:
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out: list[str] = []
    if DocumentCheckType.issuer_verification in ran:
        out.append(
            f"The issuer registry ({active_issuers} active entr{'y' if active_issuers == 1 else 'ies'}) was assumed to be "
            f"current and accurate as of {stamp}. An issuer that is absent from it is reported as “not in registry”, "
            "which is a prompt to verify the issuer — not a finding that the issuer is fraudulent."
        )
    if references:
        who = "; ".join(f"‘{name}’ (set by {by})" for name, by in references)
        out.append(
            f"Reference signature(s) were set manually by a reviewer and assumed to be correctly attributed and genuine: {who}. "
            "The system does not verify the reference itself."
        )
    uncertain = [
        f"{s.filename}: {f.name} ({f.confidence * 100:.0f}% confidence)"
        for s in sections for f in s.fields if f.uncertain and f.value != "Not found" and f.confidence is not None
    ]
    out.append(
        "Field values were read by OCR followed by language-model extraction and are assumed to have been read correctly. "
        "Each field carries a confidence and an “uncertain” flag. This build records that flag but does not "
        "automatically route low-confidence documents to manual review, so it is listed here for the reviewer instead: "
        + (
            "; ".join(uncertain[:8]) + (f"; and {len(uncertain) - 8} more" if len(uncertain) > 8 else "") + "."
            if uncertain else "no field was marked uncertain on this case."
        )
    )
    if multi_document:
        out.append(
            "Each document’s type (and therefore whether it is treated as a claim or as supporting evidence) was "
            "assigned by the classification model and assumed correct; it changes how date and issuer differences are weighted. "
            "Amounts were compared as stated in each document’s own currency, with no currency conversion."
        )
    if any_field_region:
        out.append(
            "Purple field highlights were located by matching extracted values back onto OCR word positions; treat their "
            "position as approximate. The comparison behind each one uses the stored, normalized values, not the highlight."
        )
    out.append(
        "The SHA-256 chain of custody shows the stored file is unchanged since upload. It does not show that the uploaded "
        "file is the issuer’s original — a document altered before upload is judged on its content, like any other."
    )
    if non_pdf:
        out.append(
            f"{non_pdf} document(s) are not PDFs; the PDF-based forensic checks (metadata, ELA, copy-move, duplicate, visual "
            "review) are not run on them, so absence of findings says nothing about those files."
        )
    out.append(
        "Risk-rule weights, severities and tier thresholds are administrator-configured and assumed to be appropriately "
        "calibrated; the score is advisory and is not a probability of fraud."
    )
    return out


# ---------------------------------------------------------------------------
# Orchestrating read
# ---------------------------------------------------------------------------

def _original_kind(document: Document) -> str:
    name = document.original_filename.lower()
    if document.content_type == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    for ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"):
        if name.endswith("." + ext):
            return ext
    return "pdf"


def _verify_original(
    storage: StorageService, document: Document
) -> tuple[HashVerification, bytes | None]:
    """Re-hash the stored original (read-only) and compare with the SHA-256
    recorded at upload — the chain-of-custody statement the report prints."""
    recorded = document.file_hash
    try:
        content = storage.download_bytes(document.blob_storage_path)
    except Exception as exc:  # noqa: BLE001 - any storage failure => "unverified", never a failed report
        return (
            HashVerification(
                recorded_sha256=recorded,
                status="unverified",
                note=f"The stored original could not be read to re-verify it: {_shorten(str(exc), 160)}",
            ),
            None,
        )
    computed = hashlib.sha256(content).hexdigest()
    status = "match" if computed == recorded else "mismatch"
    return HashVerification(recorded_sha256=recorded, status=status, computed_sha256=computed), content


_PAGE_IN_TEXT = re.compile(r"\b[Pp]ages? (\d+)")


def _cross_document_row_for(mine: list[ExceptionItem], low_count: int) -> CheckRow:
    """This document's row in the All Checks table: flagged if any material
    cross-document exception involves it."""
    if mine:
        return CheckRow(
            name=CROSS_DOCUMENT_LABEL, description=CROSS_DOCUMENT_DESC, result="flag",
            summary=_shorten("; ".join(e.text for e in mine), 260),
        )
    note = (
        f" {low_count} low-severity difference(s) expected between a claim and its evidence document are context only."
        if low_count else ""
    )
    return CheckRow(
        name=CROSS_DOCUMENT_LABEL, description=CROSS_DOCUMENT_DESC, result="pass",
        summary="No material mismatch on issuer, date or amount against the case's other documents." + note,
    )


def collect_report_data(
    db: Session,
    case: Case,
    generated_by: User,
    storage: StorageService,
    *,
    report_id: uuid.UUID,
    generated_at: datetime,
) -> ReportData:
    documents = list(
        db.execute(select(Document).where(Document.case_id == case.id).order_by(Document.created_at))
        .scalars().all()
    )
    doc_ids = [d.id for d in documents]

    # The row per (document, check_type) — exactly one each (unique
    # constraint; a re-run overwrites it, see app/services/check_store.py).
    latest_by_doc: dict[uuid.UUID, dict[DocumentCheckType, DocumentCheck]] = {d.id: {} for d in documents}
    if doc_ids:
        for check in db.execute(
            select(DocumentCheck).where(DocumentCheck.document_id.in_(doc_ids))
        ).scalars().all():
            latest_by_doc[check.document_id][check.check_type] = check

    matches_by_doc: dict[uuid.UUID, list[tuple[SignatureMatch, str]]] = {d.id: [] for d in documents}
    for match, person_name in db.execute(
        select(SignatureMatch, SignatureReference.person_name)
        .join(SignatureReference, SignatureReference.id == SignatureMatch.signature_reference_id)
        .where(SignatureMatch.case_id == case.id)
        .order_by(SignatureMatch.compared_at)
    ).all():
        matches_by_doc.setdefault(match.document_id, []).append((match, person_name))
    reference_rows = db.execute(
        select(SignatureReference.person_name, User)
        .join(User, User.id == SignatureReference.created_by)
        .where(SignatureReference.source_case_id == case.id)
    ).all()
    has_reference = bool(reference_rows)

    # --- per-document sections ----------------------------------------
    sections: list[DocumentSection] = []
    for index, doc in enumerate(documents, 1):
        checks = latest_by_doc[doc.id]
        annotations, exceptions = derive_findings(doc, index, checks, matches_by_doc[doc.id])
        verification, content = _verify_original(storage, doc)
        note = None
        if doc.processing_status == DocumentProcessingStatus.failed:
            note = f"Extraction failed: {_shorten(doc.processing_error or 'unknown error', 200)}"
        elif doc.processing_status != DocumentProcessingStatus.complete:
            note = "Text extraction had not finished when this report was generated."
        sections.append(
            DocumentSection(
                document_id=doc.id,
                filename=doc.original_filename,
                document_type=humanize(doc.document_type) if doc.document_type else "Not yet classified",
                uploaded_at=_aware(doc.created_at),
                hash=verification,
                processing_note=note,
                check_rows=_check_rows(checks, matches_by_doc[doc.id]),
                annotations=annotations,
                fields=extracted_field_rows(doc.extracted_fields),
                original_bytes=content,  # trimmed below once we know which documents have highlights
                original_kind=_original_kind(doc),
                index=index,
                exceptions=exceptions,
                file_size_bytes=doc.file_size_bytes,
                content_type=doc.content_type,
            )
        )

    # --- cross-document exceptions: highlighted on BOTH documents --------
    cross_exceptions: list[ExceptionItem] = []
    cross: list[CrossRow] | None = None
    low_cross = 0
    if len(documents) >= 2:
        findings = list(
            db.execute(
                select(CrossDocumentFinding)
                .where(CrossDocumentFinding.case_id == case.id)
                .order_by(CrossDocumentFinding.created_at)
            ).scalars().all()
        )
        cross = cross_document_rows(documents, findings)
        documents_by_id = {str(d.id): d for d in documents}
        sections_by_id = {str(s.document_id): s for s in sections}
        for finding in findings:
            if finding.severity.value not in EXCEPTION_SEVERITIES:
                low_cross += 1
                continue
            regions = cross_document_regions(finding.field_name, finding.document_ids, documents_by_id)
            linked: list[PageAnnotation] = []
            for region in regions:
                box = _valid_box(region["bounding_box"])
                section = sections_by_id.get(region["document_id"])
                if box is None or section is None:
                    continue
                annotation = PageAnnotation(
                    page=box[0], kind="field", check_label=CROSS_DOCUMENT_LABEL, caption=region["caption"],
                    description=(
                        f"{humanize(finding.field_name)} mismatch. This document shows {region['value']}; "
                        + "; ".join(f"{o['document_filename']} shows {o['value']}" for o in region["other"])
                        + "."
                    ),
                    box=box[1], check_type="cross_document_consistency",
                    exception_text=f"{humanize(finding.field_name)} mismatch",
                )
                section.annotations.append(annotation)
                linked.append(annotation)
            shown = [
                (sections_by_id[str(i)], format_field_value(
                    finding.field_name,
                    ((documents_by_id[str(i)].extracted_fields or {}).get("core_fields") or {}).get(finding.field_name),
                ))
                for i in (finding.document_ids or []) if str(i) in sections_by_id
            ]
            item = ExceptionItem(
                involved=[str(sec.document_id) for sec, _ in shown],
                source=CROSS_DOCUMENT_LABEL,
                text=f"{humanize(finding.field_name)} mismatch: "
                + " vs ".join(f"{sec.document_type} ({sec.filename}) {value}" for sec, value in shown),
                detail=_shorten(finding.description, 320),
                severity=finding.severity.value,
                annotations=linked,
            )
            cross_exceptions.append(item)

    for section in sections:
        if len(documents) >= 2:
            section.check_rows.append(
                _cross_document_row_for(
                    [e for e in cross_exceptions if str(section.document_id) in e.involved], low_cross
                )
            )
        # Only documents with highlights need their original kept for rendering.
        if not section.annotations:
            section.original_bytes = None

    ordered_regions = assign_region_ids(sections)

    # --- risk ----------------------------------------------------------
    assessment = latest_assessment(db, case.company_id, case.id)
    risk: RiskSummary | None = None
    findings_rows: list[FindingRow] = []
    if assessment is not None:
        snapshot = assessment.risk_rules_version_snapshot or {}
        versions: dict[tuple[str, int], float] = {}
        for rule in snapshot.get("rules", []):
            versions[(str(rule.get("rule_id")), int(rule.get("version", 1)))] = float(rule.get("weight", 0))
        risk = RiskSummary(
            tier=assessment.tier.value,
            score=assessment.score,
            raw_score=assessment.raw_score,
            computed_at=_aware(assessment.computed_at),
            assessment_id=assessment.id,
            # The stored, already-rendered reason text — not regenerated.
            reasons=[
                ReasonRow(
                    severity=str(r.get("severity", "")),
                    weight=float(r.get("weight", 0)),
                    text=str(r.get("reason", "")),
                    document=r.get("document_filename"),
                )
                for r in (assessment.triggered_reasons or [])
            ],
            rule_versions=sorted((rid, ver, w) for (rid, ver), w in versions.items()),
            thresholds=dict(snapshot.get("thresholds", {})),
            group_caps=dict(snapshot.get("group_caps") or {}),
        )
        check_labels = {ct.value: label for ct, label, _ in CHECK_CATALOG}
        check_labels["cross_document_consistency"] = CROSS_DOCUMENT_LABEL
        check_labels["signature_comparison"] = SIGNATURE_COMPARISON_LABEL
        filenames = {str(s.document_id): s.filename for s in sections}
        short_by_doc = {
            str(doc.id): summarize_document_checks(list(latest_by_doc[doc.id].values())) for doc in documents
        }
        for r in sorted(assessment.triggered_reasons or [], key=lambda r: -float(r.get("weight", 0))):
            check_type = str(r.get("check_type") or "")
            document_id = str(r["document_id"]) if r.get("document_id") else None
            linked = [
                a for s in sections for a in s.annotations
                if a.check_type == check_type and (document_id is None or str(s.document_id) == document_id)
            ]
            text = str(r.get("reason", ""))
            mentioned = _PAGE_IN_TEXT.search(text)
            title, short = risk_reason_short(r, short_by_doc.get(document_id or ""))
            findings_rows.append(
                FindingRow(
                    severity=str(r.get("severity", "")),
                    weight=float(r.get("weight", 0)),
                    check=check_labels.get(check_type, humanize(check_type) if check_type else "—"),
                    document=r.get("document_filename") or filenames.get(document_id or ""),
                    page=int(mentioned.group(1)) if mentioned else (linked[0].page if linked else None),
                    text=text,
                    annotations=linked,
                    title=title,
                    short=short,
                )
            )
    pipeline = pipeline_status(db, case.company_id, case.id)

    # --- reviewer decision ---------------------------------------------
    actions = list(
        db.execute(select(CaseAction).where(CaseAction.case_id == case.id).order_by(CaseAction.created_at))
        .scalars().all()
    )
    action_actor_ids = {a.actor_user_id for a in actions if a.actor_user_id}
    action_actors = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(action_actor_ids))).scalars().all()}
        if action_actor_ids else {}
    )

    def actor_of(action: CaseAction) -> str | None:
        return _actor_label(action_actors.get(action.actor_user_id), action.actor_role) if action.actor_user_id else None

    decision: DecisionInfo | None = None
    if case.status in (CaseStatus.approved, CaseStatus.rejected, CaseStatus.closed):
        decided = [a for a in actions if a.action_type in (CaseActionType.approve, CaseActionType.reject)]
        if decided:
            last = decided[-1]
            decision = DecisionInfo(
                label="Approved" if last.action_type == CaseActionType.approve else "Rejected",
                by=actor_of(last), at=_aware(last.created_at), note=last.notes,
            )
    elif case.status == CaseStatus.auto_approved:
        decision = DecisionInfo(
            label="Auto-approved", by=None, at=None,
            note="Cleared automatically by the risk engine (low risk); no reviewer decision was required.",
        )
    escalations = [a for a in actions if a.action_type == CaseActionType.escalate]
    escalation = (
        EscalationInfo(by=actor_of(escalations[-1]), at=_aware(escalations[-1].created_at), note=escalations[-1].notes)
        if escalations else None
    )

    # --- audit / coverage / appendix / assumptions ------------------------
    audit, truncated = audit_rows(db, case.company_id, case.id)
    submitter = db.get(User, case.submitted_by_user_id)
    types = sorted({s.document_type for s in sections})
    ran = {ct for checks in latest_by_doc.values() for ct in checks}
    non_pdf = sum(1 for s in sections if s.original_kind != "pdf")
    fields_located = any(
        f.get("bounding_box")
        for d in documents for f in ((d.extracted_fields or {}).get("core_fields") or {}).values()
        if isinstance(f, dict)
    )
    active_issuers = db.execute(
        select(func.count())
        .select_from(IssuerRegistry)
        .where(IssuerRegistry.is_active.is_(True), IssuerRegistry.company_id == case.company_id)
    ).scalar_one()
    generated = _aware(generated_at)
    return ReportData(
        report_id=report_id,
        generated_at=generated,
        generated_by_name=_user_name(generated_by),
        generated_by_role=role_label(generated_by.role),
        case_id=case.id,
        case_number=case.case_number,
        case_type=humanize(case.case_type.value),
        case_status=humanize(case.status.value),
        submitter=_user_name(submitter),
        submitted_at=_aware(case.created_at),
        document_types=types,
        documents=sections,
        risk=risk,
        pipeline_pending=list(pipeline.pending),
        decision=decision,
        escalation=escalation,
        cross_document=cross,
        audit=audit,
        audit_truncated=truncated,
        coverage=coverage_rows(
            documents, latest_by_doc,
            has_signature_reference=has_reference,
            signature_comparisons=sum(len(v) for v in matches_by_doc.values()),
        ),
        exceptions=[e for s in sections for e in s.exceptions] + cross_exceptions,
        linked_cases=[
            LinkedCaseRow(link.case_number, link.reasons)
            for link in find_linked_cases(db, case.company_id, case.id)
        ],
        findings=findings_rows,
        technical=technical_blocks(ran, multi_document=len(documents) >= 2, fields_located=fields_located),
        assumptions=build_assumptions(
            sections=sections, ran=ran,
            references=[(name, _user_name(user)) for name, user in reference_rows],
            active_issuers=active_issuers, generated_at=generated,
            multi_document=len(documents) >= 2,
            any_field_region=any(a.kind == "field" for a in ordered_regions),
            non_pdf=non_pdf,
        ),
    )
