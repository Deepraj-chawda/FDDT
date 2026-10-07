"""
Runs the ghost-content check (app/services/forensics/ghost_content.py) on
documents processed before it existed, so their case page and report show
its result ("Not applicable: no text layer", a pass, or findings) instead of
"Did not run". Only PDFs with no ghost_content row are touched (or every PDF
with --all); nothing else is re-run. A case whose result flags is re-scored,
as a new assessment.

Load-test companies are skipped unless --include-loadtest.

Usage (from backend/):
    python -m scripts.backfill_ghost_content            # documents without the check
    python -m scripts.backfill_ghost_content --case CASE-346FA23E
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db.session import system_session, tenant_session
from app.models.case import Case
from app.models.company import Company
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.check_store import save_check
from app.services.forensics.ghost_content import add_vision_hints, analyze_ghost_content, exclude_signature_regions
from app.services.risk_scoring_service import score_case
from app.services.storage_service import StorageOperationError, get_storage_service_for_task
from app.tasks.document_checks import detected_regions
from app.tasks.tampering_checks_task import _crop_reader, _erased_content_guesser, _is_pdf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="only this case number")
    parser.add_argument("--all", action="store_true", help="re-run on documents that already have the check")
    parser.add_argument(
        "--include-loadtest", action="store_true",
        help="also the load-test companies' documents (their files usually no longer exist)",
    )
    args = parser.parse_args()

    with system_session() as db:
        query = select(Document.id, Document.company_id, Document.case_id)
        if args.case:
            query = query.join(Case, Case.id == Document.case_id).where(Case.case_number == args.case)
        if not args.include_loadtest:
            query = query.join(Company, Company.id == Document.company_id).where(~Company.name.startswith("Loadtest"))
        if not args.all:
            done = select(DocumentCheck.document_id).where(DocumentCheck.check_type == DocumentCheckType.ghost_content)
            query = query.where(Document.id.not_in(done))
        rows = db.execute(query).all()

    storage = get_storage_service_for_task()
    reader, guess = _crop_reader(), _erased_content_guesser()
    rescore: set[tuple] = set()
    for n, (document_id, company_id, case_id) in enumerate(rows, 1):
        with tenant_session(company_id) as db:
            document = db.get(Document, document_id)
            if document is None or not _is_pdf(document):
                continue
            try:
                pdf = storage.download_bytes(document.blob_storage_path)
            except (StorageOperationError, ValueError) as exc:  # missing or malformed stored file
                print(f"[{n}/{len(rows)}] {document.original_filename}: skipped, file not readable ({exc})")
                continue
            result = exclude_signature_regions(analyze_ghost_content(pdf, read_image=reader), detected_regions(db, document))
            if guess is not None:
                result = add_vision_hints(result, pdf, guess)
            save_check(db, document=document, check_type=DocumentCheckType.ghost_content,
                       status=DocumentCheckStatus.completed, result=result)
            db.commit()
            print(f"[{n}/{len(rows)}] {document.original_filename}: {result['result']}", flush=True)
            if result["result"] == "flag":
                rescore.add((company_id, case_id))
    for company_id, case_id in rescore:
        with tenant_session(company_id) as db:
            assessment = score_case(db, company_id, case_id)
            db.commit()
            print(f"re-scored case {case_id}: {assessment.score} ({assessment.tier.value})")


if __name__ == "__main__":
    main()
