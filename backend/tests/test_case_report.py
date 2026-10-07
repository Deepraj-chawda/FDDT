"""
Per-case PDF report (app/services/case_report_*.py, app/api/case_reports.py).

The originals are real PDFs generated in-test and held in the fake storage,
so the chain-of-custody re-hash and the page rendering exercise real bytes.
"""
import hashlib
import re
import unicodedata
import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.case import CaseStatus, RiskTier
from app.models.case_action import CaseAction, CaseActionType
from app.models.case_report import CaseReport
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.cross_document_finding import CrossDocumentFinding, FindingSeverity
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.core.config import APP_FULL_NAME, APP_NAME, settings
from app.models.signature_reference import SignatureReference
from app.services.case_report_service import generate_case_report
from app.services.forensics import copy_move, ela
from tests.helpers_risk import make_case

_RED = (0.863, 0.149, 0.149)  # --destructive #DC2626
_BLUE = (0.145, 0.388, 0.922)  # --info #2563EB


def _pdf_bytes(pages: int = 3, label: str = "Invoice") -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"{label} page {i + 1} body text", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def _add_document(
    db, fake_storage, case, user, name, content: bytes, *, fields: dict | None = None,
    document_type: str = "vendor_invoice",
) -> Document:
    path = f"{case.id}/{uuid.uuid4()}_{name}"
    fake_storage.uploads[path] = content
    doc = Document(
        case_id=case.id,
        uploaded_by_user_id=user.id,
        original_filename=name,
        blob_storage_path=f"https://fake.blob.core.windows.net/documents/{path}",
        file_hash=hashlib.sha256(content).hexdigest(),
        content_type="application/pdf",
        document_type=document_type,
        processing_status=DocumentProcessingStatus.complete,
        extracted_fields=fields,
    )
    db.add(doc)
    db.commit()
    return doc


def _add_check(db, doc, check_type, result="pass", details=None) -> None:
    db.add(
        DocumentCheck(
            document_id=doc.id,
            check_type=check_type,
            status=DocumentCheckStatus.completed,
            result={"result": result, "details": details if details is not None else []},
        )
    )
    db.commit()


def _box(page, x=0.1, y=0.2, w=0.3, h=0.1):
    return {"page": page, "x": x, "y": y, "width": w, "height": h}


def _add_assessment(db, case, *, tier=RiskTier.high, score=72, reasons=None, rules=None) -> CaseRiskAssessment:
    row = CaseRiskAssessment(
        case_id=case.id,
        raw_score=float(score),
        score=score,
        tier=tier,
        triggered_reasons=reasons
        if reasons is not None
        else [
            {
                "rule_id": "ela.tamper_region_detected", "rule_version": 4, "category": "forensics",
                "check_type": "error_level_analysis", "severity": "medium", "weight": 25.0,
                "reason": "'inv.pdf': 1 localized area(s) show higher recompression error.",
                "document_id": None, "document_filename": "inv.pdf",
            }
        ],
        risk_rules_version_snapshot={
            "rules": rules if rules is not None else [
                {"rule_pk": str(uuid.uuid4()), "rule_id": "ela.tamper_region_detected", "version": 4,
                 "weight": 25.0, "severity": "medium"}
            ],
            "thresholds": {"medium": 30, "high": 60},
        },
        evidence_fingerprint=uuid.uuid4().hex,
    )
    db.add(row)
    db.commit()
    return row


def _read(pdf_bytes: bytes) -> pymupdf.Document:
    return pymupdf.open("pdf", pdf_bytes)


def _all_text(pdf: pymupdf.Document) -> str:
    # MuPDF extracts "fi"/"ff"/"fl" as single ligature glyphs; NFKC undoes
    # that so plain words ("certified", "differs") can be searched for.
    return unicodedata.normalize("NFKC", "\n".join(p.get_text() for p in pdf))


def _generate(db, fake_storage, case, user) -> tuple[CaseReport, pymupdf.Document]:
    report = generate_case_report(db, case, user, fake_storage)
    stored = fake_storage.uploads[report.blob_url.rsplit("/documents/", 1)[-1]]
    return report, _read(stored)


def _annotated_pages(pdf: pymupdf.Document) -> list[pymupdf.Page]:
    """The rendered evidence pages of Section 9 (each has the source image)."""
    return [p for p in pdf if "Section 9 · Document" in p.get_text() and p.get_images()]


@pytest.fixture()
def tampered_case(db_session, fake_storage, seeded_user):
    """One 3-page document with findings ONLY on page 2 (ELA + copy-move
    solid boxes, a visual-review dashed box), plus an info-only note on
    page 3 that must not cause page 3 to be rendered."""
    case = make_case(db_session, seeded_user)
    doc = _add_document(
        db_session, fake_storage, case, seeded_user, "tampered.pdf", _pdf_bytes(3),
        fields={"core_fields": {"amount": {"value": 1500.5, "currency": "USD", "confidence": 0.9, "uncertain": False}},
                "additional_fields": []},
    )
    _add_check(db_session, doc, DocumentCheckType.error_level_analysis, "flag", [
        {"finding": "recompression_error_region", "severity": "medium", "description": "ELA region.",
         "page": 2, "bounding_box": _box(2)},
    ])
    _add_check(db_session, doc, DocumentCheckType.copy_move_detection, "flag", [
        {"finding": "copy_move_cluster", "severity": "high", "description": "Cloned region.",
         "page": 2, "bounding_box": _box(2, 0.5, 0.5)},
    ])
    _add_check(db_session, doc, DocumentCheckType.visual_inconsistency_review, "flag", [
        {"finding": "visual_text_alignment", "severity": "medium", "description": "Line is misaligned.",
         "page": 2, "bounding_box": _box(2, 0.1, 0.7), "data": {"category": "text_alignment"}},
        {"finding": "ai_generation_assessment", "severity": "info", "description": "No signs.", "page": 3},
    ])
    _add_check(db_session, doc, DocumentCheckType.metadata_forensics, "pass", [])
    _add_assessment(db_session, case)
    return case, doc


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def test_only_pages_with_findings_are_rendered(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)

    annotated = _annotated_pages(pdf)
    assert len(annotated) == 1
    assert "page 2" in annotated[0].get_text()
    # The one rendered page image is the only image anywhere in the report:
    # pages 1 and 3 of the 3-page original were never rendered into it.
    assert sum(1 for p in pdf if p.get_images()) == 1


def test_solid_and_dashed_boxes_follow_the_live_ui_convention(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    page = _annotated_pages(pdf)[0]

    def near(a, b):
        return a is not None and all(abs(x - y) < 0.02 for x, y in zip(a, b))

    drawings = page.get_drawings()
    solid_red = [d for d in drawings if near(d.get("color"), _RED) and "4 3" not in (d.get("dashes") or "")]
    dashed_blue = [d for d in drawings if near(d.get("color"), _BLUE) and "4 3" in (d.get("dashes") or "")]
    assert solid_red, "ELA should be a solid red box"
    assert dashed_blue, "visual review should be a dashed box"

    text = page.get_text()
    assert "ELA — possible tamper region" in text
    assert "Copy-move — possible duplicated region" in text
    assert "approximate" in text  # dashed box's caption


def test_report_has_the_nine_sections_in_order_under_the_fddt_name(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    report, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = _all_text(pdf)

    headings = [
        "1. Case Details", "2. Executive Summary", "3. Explainable Findings", "4. All Checks", "5. Exceptions",
        "6. Limitations and Assumptions", "7. Audit Trail", "8. Appendix", "9. PDF Highlighted Regions",
    ]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions), "sections out of order"

    # product name comes from the single constant, on every page's header + metadata
    assert APP_NAME == "FDDT" and APP_FULL_NAME == "Fraud Document Detection Tool"
    for page in pdf:
        assert APP_NAME in page.get_text() and APP_FULL_NAME in page.get_text()
    assert APP_NAME in pdf.metadata["title"]
    assert [entry[1] for entry in pdf.get_toc() if entry[0] == 1][-1] == "9. PDF Highlighted Regions"

    assert case.case_number in text and str(report.id) in text and seeded_user.full_name in text
    assert "HIGH RISK" in text
    assert "1 localized area(s) show higher recompression error." in text  # stored reason text, not regenerated
    assert "Pending review" in text
    assert "1,500.50 USD" in text and doc.file_hash in text  # appendix: fields + hashes


def test_executive_summary_is_short_and_findings_hold_the_detail(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = " ".join(_all_text(pdf).split())
    summary = text[text.index("2. Executive Summary"): text.index("3. Explainable Findings")]
    assert "HIGH RISK" in summary and "Decision status: Pending review" in summary and "Why:" in summary
    assert len(summary) < 900  # a summary, not the findings list
    findings = text[text.index("3. Explainable Findings"): text.index("4. All Checks")]
    assert "ela.tamper" not in findings  # reasons are plain text...
    assert "recompression error" in findings and "Error level analysis" in findings and "inv.pdf" in findings


def test_all_checks_lists_passes_as_well_as_flags(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    checks = flat[flat.index("4. All Checks"): flat.index("5. Exceptions")]
    assert "Metadata forensics" in checks and "Pass" in checks  # a passed check is on record
    assert "Error level analysis (ELA)" in checks and "Flag" in checks


def test_exceptions_reference_the_highlight_regions_shown_in_section_9(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    exceptions = flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    regions = flat[flat.index("9. PDF Highlighted Regions"):]
    assert "Possible edited area —" in exceptions and "Copied region —" in exceptions
    for region_id in ("R1", "R2", "R3"):
        assert region_id in exceptions
        assert f"{region_id} ·" in regions or f"{region_id}." in regions  # drawn/listed in Section 9

    # ...and each cited ID is a working link to a Section 9 page
    exceptions_page = next(p for p in pdf if "5. Exceptions" in p.get_text())
    first_regions_page = next(i for i, p in enumerate(pdf) if "9. PDF Highlighted Regions" in p.get_text())
    links = [link for link in exceptions_page.get_links() if link["kind"] == pymupdf.LINK_GOTO]
    assert links and all(link["page"] >= first_regions_page for link in links)


def test_decision_and_escalation_shown(db_session, fake_storage, seeded_user, reviewer_user, tampered_case):
    case, _ = tampered_case
    case.status = CaseStatus.rejected
    db_session.add_all([
        CaseAction(case_id=case.id, actor_user_id=reviewer_user.id, action_type=CaseActionType.escalate, notes="needs a second look"),
        CaseAction(case_id=case.id, actor_user_id=reviewer_user.id, action_type=CaseActionType.reject, notes="cloned totals"),
    ])
    db_session.commit()
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = " ".join(_all_text(pdf).split())
    assert f"Rejected by {reviewer_user.full_name}" in text and "escalated by" in text
    assert "Pending review" not in text


def test_report_labels_each_actor_with_their_reviewer_tier(
    client, db_session, fake_storage, seeded_user, reviewer_headers, l2_reviewer_headers, tampered_case
):
    """Escalated by an L1, resolved by an L2: the audit trail and the decision
    line name the tier of each actor, not a generic "Reviewer"."""
    case, _ = tampered_case
    case.status = CaseStatus.pending_manual_review
    db_session.commit()
    assert client.post(f"/cases/{case.id}/escalate", json={"reason": "needs L2"}, headers=reviewer_headers).status_code == 200
    assert client.post(f"/cases/{case.id}/reject", json={"reason": "cloned totals"}, headers=l2_reviewer_headers).status_code == 200

    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = " ".join(_all_text(pdf).split())
    assert "Case escalated to L2" in text and "Rita Reviewer (Reviewer L1)" in text
    assert "Reviewer decision: rejected" in text and "Lena Level-Two (Reviewer L2)" in text
    assert "Rejected by Lena Level-Two (Reviewer L2)" in text
    assert "escalated by Rita Reviewer (Reviewer L1)" in text
    assert "(Reviewer L2)" in text  # generated-by line uses the display label, not "reviewer_l2"


def test_chain_of_custody_hash_verified_against_stored_original(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = _all_text(pdf)
    assert "VERIFIED" in text and "MISMATCH" not in text

    # Corrupt the stored original: the recorded hash no longer matches it.
    blob = doc.blob_storage_path.rsplit("/documents/", 1)[-1]
    fake_storage.uploads[blob] = fake_storage.uploads[blob] + b"\n%tampered"
    _, pdf2 = _generate(db_session, fake_storage, case, seeded_user)
    text2 = _all_text(pdf2)
    assert "MISMATCH" in text2
    assert hashlib.sha256(fake_storage.uploads[blob]).hexdigest() in text2.replace("\n", "")


def test_limitations_and_assumptions_are_complete_and_case_specific(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    _add_check(db_session, doc, DocumentCheckType.issuer_verification, "flag", {"issuer_name": "Acme"})
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    section = flat[flat.index("6. Limitations and Assumptions"): flat.index("7. Audit Trail")]

    for phrase in (
        "not a certified forensic or legal determination",
        "probabilistic assessments",
        "dashed boxes captioned",
        "solid boxes",
        "solid purple boxes",
        "cannot verify facts external to the document",
        "does not guarantee that a document is genuine",
        "Check coverage on this case",
        "Not applicable — this case has a single document",
        "no reference signature was set for this case",
        "ela.tamper_region_detected v4 (weight 25)",  # rule version used for the score
        "medium from 30, high from 60",
        "accurate historical record",
        "Assumptions",
        "issuer registry",  # assumption present because issuer verification ran
        "does not automatically route low-confidence documents to manual review",
        "no field was marked uncertain",
        "chain of custody shows the stored file is unchanged",
    ):
        assert phrase in section, phrase
    assert "Reference signature(s) were set manually" not in section  # none set on this case


def test_assumption_names_manual_signature_references(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    ref = SignatureReference(person_name="Dr. Tariq", source_document_id=doc.id, source_case_id=case.id,
                             created_by=seeded_user.id, is_library=False)
    db_session.add(ref)
    db_session.commit()
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    assert "Reference signature(s) were set manually by a reviewer" in flat and "Dr. Tariq" in flat


def test_appendix_records_technical_parameters_actually_used(db_session, fake_storage, seeded_user, tampered_case):
    case, _ = tampered_case
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    appendix = flat[flat.index("8. Appendix"): flat.index("9. PDF Highlighted Regions")]
    assert "8.1 Extracted fields" in appendix and "8.2 Document hashes" in appendix and "8.3 Technical parameters" in appendix
    assert f"JPEG recompression quality {ela.JPEG_RECOMPRESS_QUALITY}" in appendix
    assert f"{copy_move.DETECTOR_TYPE} (OpenCV)" in appendix
    assert f"Hamming-distance threshold {settings.duplicate_hash_hamming_threshold}" not in appendix  # duplicate check didn't run here
    assert "Cross-document consistency" not in appendix  # single-document case


# ---------------------------------------------------------------------------
# Field-level exceptions (purple) and cross-document mismatches
# ---------------------------------------------------------------------------

def _box_dict(page=1, x=0.2, y=0.4, w=0.2, h=0.03):
    return {"page": page, "x": x, "y": y, "width": w, "height": h}


def _fields_with_boxes(amount, box, currency="USD", subtotal=None, subtotal_box=None):
    core = {"amount": {"value": amount, "currency": currency, "confidence": 0.9, "uncertain": False, "bounding_box": box}}
    if subtotal is not None:
        core["subtotal"] = {"value": subtotal, "currency": currency, "confidence": 0.9, "uncertain": False,
                            "bounding_box": subtotal_box}
    return {"core_fields": core, "additional_fields": []}


def _mismatch_case(db_session, fake_storage, user, *, boxes=True):
    case = make_case(db_session, user)
    invoice = _add_document(
        db_session, fake_storage, case, user, "invoice.pdf", _pdf_bytes(1, "A"),
        fields=_fields_with_boxes(9030.0, _box_dict(y=0.30) if boxes else None),
    )
    payment = _add_document(
        db_session, fake_storage, case, user, "payment.pdf", _pdf_bytes(1, "B"),
        fields=_fields_with_boxes(7250.0, _box_dict(y=0.55) if boxes else None), document_type="payment_evidence",
    )
    db_session.add(CrossDocumentFinding(
        case_id=case.id, field_name="amount", finding_type="cross_document_consistency",
        severity=FindingSeverity.high,
        description="Amount differs between 'invoice.pdf' (9030.00 USD) and 'payment.pdf' (7250.00 USD).",
        document_ids=[str(invoice.id), str(payment.id)],
    ))
    db_session.commit()
    return case, invoice, payment


def test_cross_document_mismatch_is_highlighted_in_purple_on_both_documents(db_session, fake_storage, seeded_user):
    case, _, _ = _mismatch_case(db_session, fake_storage, seeded_user)
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())

    exceptions = flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    assert "Amount mismatch: Vendor invoice (invoice.pdf) 9,030.00 USD vs Payment evidence (payment.pdf) 7,250.00 USD" in exceptions
    assert "R1" in exceptions and "R2" in exceptions  # a highlight region ID on each side

    pages = _annotated_pages(pdf)
    assert len(pages) == 2  # one page per document — BOTH documents' pages are shown
    purple = (0.486, 0.227, 0.929)  # #7C3AED
    for page, own, other in ((pages[0], "9,030.00 USD", "7,250.00 USD"), (pages[1], "7,250.00 USD", "9,030.00 USD")):
        solid_purple = [
            d for d in page.get_drawings()
            if d.get("color") and all(abs(a - b) < 0.02 for a, b in zip(d["color"], purple))
            and "4 3" not in (d.get("dashes") or "")
        ]
        assert solid_purple, "field exceptions are solid purple boxes"
        # captioned with what THIS document shows vs what the other showed
        assert f"Field mismatch: Total amount ({own} vs {other})" in page.get_text()


def test_mismatch_without_stored_field_locations_is_still_listed_just_not_drawn(db_session, fake_storage, seeded_user):
    case, _, _ = _mismatch_case(db_session, fake_storage, seeded_user, boxes=False)
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    exceptions = flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    assert "Amount mismatch:" in exceptions
    assert not _annotated_pages(pdf)
    assert "No check produced a highlight for this case" in flat


def test_low_severity_cross_document_differences_are_context_not_exceptions(db_session, fake_storage, seeded_user):
    case, invoice, payment = _mismatch_case(db_session, fake_storage, seeded_user)
    db_session.query(CrossDocumentFinding).delete()
    db_session.add(CrossDocumentFinding(
        case_id=case.id, field_name="date", finding_type="cross_document_consistency", severity=FindingSeverity.low,
        description="Date differs.", document_ids=[str(invoice.id), str(payment.id)],
    ))
    db_session.commit()
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    assert "Date mismatch" not in flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    assert "expected between a claim and its evidence document are context only" in flat


def test_flagged_field_validation_is_drawn_on_the_offending_fields(db_session, fake_storage, seeded_user):
    """Total 4520 vs subtotal 4000 + no tax: a miscalculation, highlighted on both figures."""
    case = make_case(db_session, seeded_user)
    doc = _add_document(
        db_session, fake_storage, case, seeded_user, "calc.pdf", _pdf_bytes(1),
        fields=_fields_with_boxes(4520.0, _box_dict(y=0.6), subtotal=4000.0, subtotal_box=_box_dict(y=0.5)),
    )
    from app.services.field_validation_service import validate_fields
    result = validate_fields(doc.extracted_fields)
    assert result["result"] == "flag" and result["details"]["total_tax_consistency"]["regions"]
    _add_check(db_session, doc, DocumentCheckType.field_validation, "flag", result["details"])
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    exceptions = flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    assert "Total ≠ subtotal + tax —" in exceptions and "R1" in exceptions and "R2" in exceptions
    page = _annotated_pages(pdf)[0].get_text()
    assert "Field exception: Total amount (4,520.00 USD vs expected 4,000.00)" in page


def test_legacy_validation_rows_without_regions_are_enriched_from_field_locations(db_session, fake_storage, seeded_user):
    case = make_case(db_session, seeded_user)
    doc = _add_document(
        db_session, fake_storage, case, seeded_user, "old.pdf", _pdf_bytes(1),
        fields=_fields_with_boxes(4520.0, _box_dict(y=0.6), subtotal=4000.0, subtotal_box=_box_dict(y=0.5)),
    )
    # stored the way rows were before field locations existed: flagged, no regions
    _add_check(db_session, doc, DocumentCheckType.field_validation, "flag", {
        "total_tax_consistency": {"status": "flag", "reason": "Total 4520.00 does not match subtotal 4000.00."},
    })
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    assert len(_annotated_pages(pdf)) == 1


def test_missing_required_field_is_an_exception_with_no_highlight(db_session, fake_storage, seeded_user):
    case = make_case(db_session, seeded_user)
    _add_document(
        db_session, fake_storage, case, seeded_user, "nodate.pdf", _pdf_bytes(1),
        fields={"core_fields": {
            "amount": {"value": 10.0, "currency": "USD", "confidence": 0.9, "uncertain": False},
            "date": {"value": None, "raw_text": None, "confidence": 0.0, "uncertain": False},
        }, "additional_fields": []},
    )
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    exceptions = flat[flat.index("5. Exceptions"): flat.index("6. Limitations")]
    assert "Missing required field: Date was not found on this document" in exceptions
    assert not _annotated_pages(pdf)


def test_three_colour_convention_renders_together_in_one_report(db_session, fake_storage, seeded_user):
    """Forensic solid (red/orange), model-judgment dashed, field-exception solid purple — one page."""
    case = make_case(db_session, seeded_user)
    doc = _add_document(
        db_session, fake_storage, case, seeded_user, "all.pdf", _pdf_bytes(1),
        fields=_fields_with_boxes(4520.0, _box_dict(y=0.85), subtotal=4000.0, subtotal_box=_box_dict(y=0.75)),
    )
    from app.services.field_validation_service import validate_fields
    _add_check(db_session, doc, DocumentCheckType.field_validation, "flag", validate_fields(doc.extracted_fields)["details"])
    _add_check(db_session, doc, DocumentCheckType.error_level_analysis, "flag", [
        {"finding": "recompression_error_region", "severity": "medium", "description": "ELA.", "page": 1, "bounding_box": _box(1, 0.1, 0.1)}])
    _add_check(db_session, doc, DocumentCheckType.copy_move_detection, "flag", [
        {"finding": "copy_move_cluster", "severity": "high", "description": "Clone.", "page": 1, "bounding_box": _box(1, 0.5, 0.1)}])
    _add_check(db_session, doc, DocumentCheckType.visual_inconsistency_review, "flag", [
        {"finding": "visual_text_alignment", "severity": "medium", "description": "Misaligned.", "page": 1,
         "bounding_box": _box(1, 0.1, 0.3), "data": {"category": "text_alignment"}}])
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    page = _annotated_pages(pdf)[0]

    def has(colour, dashed):
        return any(
            d.get("color") and all(abs(a - b) < 0.02 for a, b in zip(d["color"], colour))
            and (("4 3" in (d.get("dashes") or "")) == dashed)
            for d in page.get_drawings()
        )

    assert has(_RED, False)  # ELA
    assert has((0.851, 0.467, 0.024), False)  # copy-move #D97706
    assert has(_BLUE, True)  # model judgment, dashed
    assert has((0.486, 0.227, 0.929), False)  # field exception, solid purple
    text = page.get_text()
    assert all(k in text for k in ("ELA", "Copy-move", "Visual review", "Field exception"))


def test_audit_trail_comes_from_the_audit_log(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    db_session.add(AuditLog(case_id=case.id, document_id=doc.id, actor_user_id=seeded_user.id,
                            event_type="document_uploaded", event_data={"file_hash": doc.file_hash}))
    db_session.commit()
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    text = " ".join(_all_text(pdf).split())
    assert "7. Audit Trail" in text and "Document uploaded" in text and "tampered.pdf" in text


def test_arabic_extracted_values_render(db_session, fake_storage, seeded_user):
    case = make_case(db_session, seeded_user)
    _add_document(
        db_session, fake_storage, case, seeded_user, "ar.pdf", _pdf_bytes(1),
        fields={"core_fields": {"issuer": {"value": "شركة الأفق الهندسية", "confidence": 0.9, "uncertain": False}},
                "additional_fields": []},
    )
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    assert re.search(r"[؀-ۿﭐ-﻿]", _all_text(pdf))


def test_unscored_case_says_so_and_flags_preliminary(db_session, fake_storage, seeded_user):
    case = make_case(db_session, seeded_user)
    _add_document(db_session, fake_storage, case, seeded_user, "x.pdf", _pdf_bytes(1))
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    assert "Not yet scored" in flat and "Preliminary report" in flat
    assert "had not been scored when the report was generated" in flat


def test_unreadable_original_degrades_instead_of_failing(db_session, fake_storage, seeded_user, tampered_case):
    case, doc = tampered_case
    del fake_storage.uploads[doc.blob_storage_path.rsplit("/documents/", 1)[-1]]
    _, pdf = _generate(db_session, fake_storage, case, seeded_user)
    flat = " ".join(_all_text(pdf).split())
    assert "NOT VERIFIED" in flat
    assert "could not be read from storage" in flat
    assert not _annotated_pages(pdf)  # highlights listed as text instead


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_create_report_stores_under_reports_prefix_and_keeps_history(
    client, db_session, fake_storage, reviewer_headers, reviewer_user, tampered_case
):
    case, doc = tampered_case
    original_blob = doc.blob_storage_path.rsplit("/documents/", 1)[-1]
    original_before = fake_storage.uploads[original_blob]

    first = client.post(f"/cases/{case.id}/reports", headers=reviewer_headers)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["generated_by_name"] == reviewer_user.full_name
    assert body["risk_tier"] == "high" and body["risk_score"] == 72
    # Company- and case-prefixed (storage-layer tenant isolation).
    blob = f"companies/{case.company_id}/cases/{case.id}/reports/{body['id']}.pdf"
    assert body["download_url"].startswith(f"https://fake.blob.core.windows.net/documents/{blob}")
    assert body["page_count"] > 3

    assert blob in fake_storage.uploads
    assert hashlib.sha256(fake_storage.uploads[blob]).hexdigest() == body["report_sha256"]
    # Original document bytes are untouched by report generation.
    assert fake_storage.uploads[original_blob] == original_before

    row = db_session.execute(select(CaseReport).where(CaseReport.id == uuid.UUID(body["id"]))).scalar_one()
    assert row.case_id == case.id and row.generated_by_user_id == reviewer_user.id
    events = db_session.execute(select(AuditLog).where(AuditLog.event_type == "case_report_generated")).scalars().all()
    assert len(events) == 1 and events[0].event_data["report_id"] == body["id"]

    second = client.post(f"/cases/{case.id}/reports", headers=reviewer_headers)
    assert second.status_code == 201
    history = client.get(f"/cases/{case.id}/reports", headers=reviewer_headers).json()
    assert [r["id"] for r in history] == [second.json()["id"], body["id"]]  # newest first, none overwritten
    assert blob in fake_storage.uploads


def test_submitters_cannot_export_or_list_reports(client, plain_headers, tampered_case):
    case, _ = tampered_case
    assert client.post(f"/cases/{case.id}/reports", headers=plain_headers).status_code == 403
    assert client.get(f"/cases/{case.id}/reports", headers=plain_headers).status_code == 403


def test_report_requires_auth_and_existing_case(client, reviewer_headers, tampered_case):
    case, _ = tampered_case
    assert client.post(f"/cases/{case.id}/reports").status_code == 401
    assert client.post(f"/cases/{uuid.uuid4()}/reports", headers=reviewer_headers).status_code == 404
    assert client.get(f"/cases/{uuid.uuid4()}/reports", headers=reviewer_headers).status_code == 404
