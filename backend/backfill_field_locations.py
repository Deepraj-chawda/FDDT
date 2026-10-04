"""
Backfill: record WHERE each extracted field sits on the page, for documents
extracted before field locations were persisted (see
app/services/field_locator_service.py).

New uploads get their field boxes automatically at extraction time. This
script covers older ones: it re-runs only the Azure Document Intelligence
Layout call (for the word/line geometry) and matches the ALREADY-STORED
values onto it. It does not re-run the LLM, does not change any extracted
value, does not touch any check result or risk assessment, and never writes
to the original file in Blob Storage.

Usage (from backend/, venv active):
    python backfill_field_locations.py                 # every document lacking boxes
    python backfill_field_locations.py CASE-30720157   # just these case numbers
    python backfill_field_locations.py --dry-run ...   # report, change nothing

Costs one OCR call per document processed. Safe to re-run: a document whose
fields already carry boxes is skipped.
"""
import copy
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.case import Case
from app.models.document import Document, DocumentProcessingStatus
from app.services.audit_service import record_event
from app.services.field_locator_service import attach_field_locations
from app.services.ocr_service import get_ocr_service
from app.services.storage_service import get_storage_service_for_task


def _has_boxes(extracted: dict) -> bool:
    fields = list((extracted.get("core_fields") or {}).values()) + list(extracted.get("additional_fields") or [])
    return any(isinstance(f, dict) and f.get("bounding_box") for f in fields)


def main(argv: list[str]) -> None:
    dry_run = "--dry-run" in argv
    case_numbers = [a for a in argv if not a.startswith("--")]

    storage = get_storage_service_for_task()
    ocr = get_ocr_service()
    with SessionLocal() as db:
        stmt = (
            select(Document)
            .join(Case, Case.id == Document.case_id)
            .where(Document.processing_status == DocumentProcessingStatus.complete)
            .order_by(Case.created_at, Document.created_at)
        )
        if case_numbers:
            stmt = stmt.where(Case.case_number.in_(case_numbers))

        done = skipped = failed = 0
        for doc in db.execute(stmt).scalars().all():
            if not doc.extracted_fields or _has_boxes(doc.extracted_fields):
                skipped += 1
                continue
            if dry_run:
                print(f"would locate fields of {doc.original_filename}")
                continue
            try:
                url = storage.get_download_url(doc.blob_storage_path, expires_in_minutes=30)
                pages = ocr.analyze_url(url).pages
                updated = copy.deepcopy(doc.extracted_fields)
                located = attach_field_locations(pages, updated)
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the rest
                print(f"FAILED {doc.original_filename}: {exc}")
                failed += 1
                continue
            doc.extracted_fields = updated  # a new dict, so the JSON column registers the change
            record_event(
                db, "field_locations_backfilled",
                case_id=doc.case_id, document_id=doc.id, event_data={"fields_located": located},
            )
            db.commit()
            print(f"{doc.original_filename}: located {located} field(s)")
            done += 1
        print(f"Located fields on {done} document(s); skipped {skipped}; failed {failed}.")


if __name__ == "__main__":
    main(sys.argv[1:])
