"""
Re-assesses an existing case with the current check code, as a NEW
assessment (case_risk_assessments is append-only: earlier assessments and the
reports built from them are never changed), and optionally generates a new
case report.

What is re-run for each PDF in the case:
  - OCR (Azure Document Intelligence) — line items read from the layout and
    font estimates need it; it is not stored, so it is taken again
  - extraction post-processing (app/services/line_item_parsing.py) on top of
    the case's STORED LLM extraction
  - metadata forensics, ELA, copy-move, font consistency, duplicate detection
  - field validation and issuer verification
  - the vision review's region filters, applied to the STORED vision review
    with the STORED signature/stamp detection
The model outputs already recorded for the case (LLM extraction, vision
review, signature/stamp detection) are deliberately reused rather than
requested again: a new model call answers differently run to run, and this
re-assessment is meant to show what the changed checks do on the same
evidence. Then the case is scored (a new assessment, current rule versions).

Usage (from backend/):
    python -m scripts.reassess_case CASE-DB43653A CASE-DE627FA2 --report-by "test1Reviewer L2"
"""
from __future__ import annotations

import argparse
import copy

from sqlalchemy import select

from app.db.session import system_session, tenant_session
from app.models.case import Case
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.user import User
from app.services.audit_service import record_event
from app.services.case_report_service import generate_case_report
from app.services.check_store import save_check
from app.services.field_validation_service import validate_fields
from app.services.forensics.copy_move import run_copy_move_check
from app.services.forensics.duplicate_check import run_duplicate_check
from app.services.forensics.ela import run_ela_check
from app.services.forensics.font_consistency import analyze_font_consistency
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.issuer_service import verify_issuer
from app.services.line_item_parsing import enrich_extracted_fields
from app.services.ocr_service import get_ocr_service
from app.services.risk_scoring_service import score_case
from app.services.storage_service import get_storage_service_for_task
from app.services.visual_inconsistency_service import apply_region_filters


def _stored(db, document_id, check_type):
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document_id,
            DocumentCheck.check_type == check_type,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalar_one_or_none()
    return copy.deepcopy(check.result) if check is not None else None


def reassess(case_number: str, report_by: str | None) -> None:
    with system_session() as db:
        case = db.execute(select(Case).where(Case.case_number == case_number)).scalar_one()
        company_id, case_id = case.company_id, case.id
    storage = get_storage_service_for_task()
    with tenant_session(company_id) as db:
        case = db.get(Case, case_id)
        documents = db.execute(select(Document).where(Document.case_id == case_id)).scalars().all()
        for doc in documents:
            pdf = storage.download_bytes(doc.blob_storage_path)
            pages = render_pdf_pages(pdf)
            ocr = get_ocr_service().analyze_url(storage.get_download_url(doc.blob_storage_path, expires_in_minutes=30))
            doc.extracted_fields = enrich_extracted_fields(
                doc.extracted_fields or {}, ocr_text=ocr.text, ocr_tables=ocr.tables, pdf_bytes=pdf
            )
            results = {
                DocumentCheckType.metadata_forensics: analyze_pdf_metadata(pdf),
                DocumentCheckType.error_level_analysis: run_ela_check(pages),
                DocumentCheckType.copy_move_detection: run_copy_move_check(pages),
                DocumentCheckType.font_consistency: analyze_font_consistency(pdf, ocr.pages),
                DocumentCheckType.duplicate_detection: run_duplicate_check(
                    db, company_id=company_id, document_id=doc.id, pages=pages
                ),
                DocumentCheckType.field_validation: validate_fields(doc.extracted_fields, ocr_text=doc.ocr_text),
                DocumentCheckType.issuer_verification: verify_issuer(
                    db, company_id, doc.extracted_fields, document_type=doc.document_type
                ),
            }
            visual = _stored(db, doc.id, DocumentCheckType.visual_inconsistency_review)
            if visual is not None:
                signature = _stored(db, doc.id, DocumentCheckType.signature_stamp_detection)
                results[DocumentCheckType.visual_inconsistency_review] = apply_region_filters(visual, signature, pages)
            for check_type, result in results.items():
                save_check(db, document=doc, check_type=check_type, status=DocumentCheckStatus.completed, result=result)
            record_event(
                db, "document_reassessed", case_id=case_id, document_id=doc.id,
                event_data={"checks": sorted(c.value for c in results), "reused": [
                    "llm_extraction", "visual_inconsistency_review", "signature_stamp_detection",
                ]},
            )
        db.commit()
        assessment = score_case(db, company_id, case_id)
        db.commit()
        print(f"{case_number}: assessment {assessment.id} score {assessment.score} ({assessment.tier.value}), "
              f"raw {assessment.raw_score:g}")
        if report_by:
            user = db.execute(select(User).where(User.full_name == report_by)).scalars().first()
            report = generate_case_report(db, case, user, storage)
            db.commit()
            print(f"{case_number}: report {report.id} ({report.page_count} pages) {report.blob_url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="+")
    parser.add_argument("--report-by", help="full name of the user the new report is generated as")
    args = parser.parse_args()
    for case_number in args.cases:
        reassess(case_number, args.report_by)


if __name__ == "__main__":
    main()
