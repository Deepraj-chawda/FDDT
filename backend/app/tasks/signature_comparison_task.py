"""
Celery task: in-case signature comparison (SPECIFICATION.md §2.3).

`run_signature_comparison` is enqueued immediately after a reviewer
creates a signature_references row via the API (app/api/signatures.py).
It compares the new reference's cropped signature against the detected
signature regions on every OTHER document in the same case — storing
each result as a signature_matches row with comparison_scope='in_case'.

Scope intentionally limited to the same case only for this phase.
Library references (is_library=True) are data-collected here but NOT
used to trigger any cross-case comparison logic — that is explicitly
not built yet (SPECIFICATION.md §2.3: "Do NOT build any cross-case matching
logic against it yet. This is purely data collection for a future phase.")

Documents with no detected signature, or whose detection check hasn't
completed yet, are silently skipped — this is not an error.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.signature_match import ComparisonScope, SignatureMatch, SignatureMatchResult
from app.models.signature_reference import SignatureReference
from app.services.audit_service import record_event
from app.services.llm_service import LLMConfigurationError, LLMOperationError, get_llm_service
from app.services.risk_scoring_service import request_case_scoring
from app.services.signature_comparison_service import (
    crop_and_upload_signature,
    download_and_encode_signature,
    find_signature_reuse,
)
from app.services.storage_service import get_storage_service_for_task
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_signature_reference

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _store_match(db, match: SignatureMatch) -> bool:
    """Insert one comparison result unless that (reference, document, scope)
    pair already has one (unique constraint) — a retried or concurrent run of
    this task must not store the same comparison twice. Savepoint per row so a
    clash doesn't discard the rest of the batch."""
    try:
        with db.begin_nested():
            db.add(match)
            db.flush()
        return True
    except IntegrityError:
        return False


def _latest_detection_details(db, document_id: uuid.UUID) -> dict | None:
    """`details` of the completed signature_stamp_detection check for this
    document (there is one row per check type), or None if there isn't one."""
    check = db.execute(
        select(DocumentCheck)
        .where(
            DocumentCheck.document_id == document_id,
            DocumentCheck.check_type == DocumentCheckType.signature_stamp_detection,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalar_one_or_none()  # one row per (document, check_type)

    if check is None or not isinstance(check.result, dict):
        return None
    details = check.result.get("details")
    return details if isinstance(details, dict) else None


def _get_detected_signature_bbox(
    db, document_id: uuid.UUID, kind: str | None = None
) -> dict | None:
    """Bounding box of the region to crop on this document, or None.

    The detection task stores every located region in `details.detected`
    (each with a `kind` of signature/stamp) plus a single `details.bounding_box`
    for its "primary" one. With no `kind` that primary box is returned.
    With a `kind`, the most confident region of that kind is returned so a
    signature reference is compared with a signature and a stamp with a stamp;
    if detection recorded regions but none of that kind, there is nothing
    comparable (None). Results stored before `detected` existed only have the
    primary box, which is used as-is."""
    details = _latest_detection_details(db, document_id)
    if details is None:
        return None
    detected = details.get("detected")
    if kind and isinstance(detected, list) and detected:
        of_kind = [d for d in detected if d.get("kind") == kind and d.get("bounding_box")]
        if not of_kind:
            return None
        rank = {"low": 0, "medium": 1, "high": 2}
        return max(of_kind, key=lambda d: rank.get(d.get("confidence"), 0))["bounding_box"]
    return details.get("bounding_box")  # May be None if detection found no signature


def _box_iou(a: dict, b: dict) -> float:
    ix = max(0.0, min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"]))
    iy = max(0.0, min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["width"] * a["height"] + b["width"] * b["height"] - inter
    return inter / union if union > 0 else 0.0


def _infer_reference_kind(db, ref: SignatureReference) -> str | None:
    """"signature" / "stamp" for a reference, or None if it can't be told.

    The reference row doesn't record what the reviewer drew around, so infer it
    from the source document's own detection: the detected region overlapping
    the reference box most (on the same page) names the kind."""
    if not isinstance(ref.bounding_box, dict):
        return None
    details = _latest_detection_details(db, ref.source_document_id)
    detected = (details or {}).get("detected")
    if not isinstance(detected, list):
        return None
    best_kind, best_iou = None, 0.1  # below this overlap it's a guess, not evidence
    for region in detected:
        box = region.get("bounding_box")
        if not isinstance(box, dict) or box.get("page") != ref.bounding_box.get("page"):
            continue
        iou = _box_iou(ref.bounding_box, box)
        if iou > best_iou:
            best_kind, best_iou = region.get("kind"), iou
    return best_kind


def _try_download(storage, url: str) -> bytes | None:
    try:
        data = storage.download_bytes(url)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, (bytes, bytearray)) else None


@celery_app.task(name="run_signature_comparison")
def run_signature_comparison(reference_id: str, company_id: str | None = None) -> None:
    """Compare a newly-created signature reference against all other
    documents in the same case that have a detected signature region.

    Triggered by POST /cases/{case_id}/documents/{document_id}/
    signature-references immediately after the reference row is committed.
    Results are stored as signature_matches rows (comparison_scope=in_case).

    Library references (is_library=True) run through exactly the same
    in-case comparison — the is_library flag only controls whether the
    reference data is also available for future library-wide comparison,
    not whether in-case comparison runs.
    """
    company_uuid = resolve_company_for_signature_reference(reference_id, company_id)
    if company_uuid is None:
        return  # stale task
    db = open_task_session(SessionLocal, company_uuid)
    try:
        ref = db.execute(
            select(SignatureReference).where(
                SignatureReference.id == uuid.UUID(reference_id),
                SignatureReference.company_id == company_uuid,
            )
        ).scalar_one_or_none()

        if ref is None:
            return  # Stale task — reference was deleted or ID is wrong.

        if not ref.signature_image_url:
            # Crop upload failed at reference-creation time — can't compare.
            record_event(
                db,
                "signature_comparison_skipped",
                case_id=ref.source_case_id,
                document_id=ref.source_document_id,
                event_data={
                    "reason": "reference_image_unavailable",
                    "reference_id": str(ref.id),
                },
            )
            db.commit()
            return

        # Find all OTHER documents in the same case (excluding the reference
        # source), minus any already compared against this reference. This
        # task runs once at reference creation and again whenever another
        # document's detection finishes (app/tasks/signature_detection_task.py),
        # so it must be idempotent.
        already_compared = set(
            db.execute(
                select(SignatureMatch.document_id).where(
                    SignatureMatch.company_id == company_uuid,
                    SignatureMatch.signature_reference_id == ref.id,
                    SignatureMatch.comparison_scope == ComparisonScope.in_case,
                )
            ).scalars().all()
        )
        other_documents = [
            d
            for d in db.execute(
                select(Document).where(
                    Document.company_id == company_uuid,
                    Document.case_id == ref.source_case_id,
                    Document.id != ref.source_document_id,
                )
            ).scalars().all()
            if d.id not in already_compared
        ]

        # Only documents whose detection has finished AND located a signature
        # are comparable; the rest are skipped cleanly (no row, no error).
        # Documents still awaiting detection get picked up when that finishes.
        # Compare like with like: a signature reference against each document's
        # signature region, a stamp against a stamp (None kind = can't tell, so
        # the document's primary region is used).
        reference_kind = _infer_reference_kind(db, ref)
        comparable = [
            (doc, bbox)
            for doc in other_documents
            if (bbox := _get_detected_signature_bbox(db, doc.id, reference_kind)) is not None
        ]

        if not comparable:
            # Nothing new to compare (no other documents, none with a detected
            # signature yet, or all already done) — not an error, and not worth
            # an audit row on every idempotent re-run.
            return

        try:
            llm_service = get_llm_service()
        except LLMConfigurationError:
            # Vision model not configured: each target that isn't a pixel-identical
            # reuse (which needs no model) gets cannot_determine below rather than
            # the whole task failing silently.
            llm_service = None

        storage = get_storage_service_for_task()

        # Encode reference image once; reused for every target document.
        ref_data_uri = download_and_encode_signature(storage, ref.signature_image_url)
        if ref_data_uri is None:
            record_event(
                db,
                "signature_comparison_skipped",
                case_id=ref.source_case_id,
                document_id=ref.source_document_id,
                event_data={
                    "reason": "reference_image_download_failed",
                    "reference_id": str(ref.id),
                },
            )
            db.commit()
            return

        # Inputs for the pixel-identical reuse check, fetched once. Handwritten
        # signatures only — an identical rubber stamp is normal, not a flag.
        check_reuse = reference_kind == "signature"
        ref_image_bytes = _try_download(storage, ref.signature_image_url) if check_reuse else None
        source_doc = db.get(Document, ref.source_document_id) if check_reuse else None
        ref_pdf_bytes = (
            _try_download(storage, source_doc.blob_storage_path) if source_doc is not None else None
        )

        comparisons_attempted = 0
        comparisons_stored = 0
        reuse_found = 0

        for doc, target_bbox in comparable:
            # Commit what's stored so far: each iteration crops/uploads and
            # calls the vision model, and no connection should be held then.
            db.commit()
            comparisons_attempted += 1

            # Crop the target's detected signature region from the raw PDF.
            # A fresh blob name per attempt: uploads are overwrite=False, so a
            # path derived from (reference, document) would make any retry or
            # concurrent run fail the upload and wrongly record cannot_determine.
            target_image_url = crop_and_upload_signature(
                storage,
                doc.blob_storage_path,
                target_bbox,
                reference_id=uuid.uuid4(),
                company_id=company_uuid,
                case_id=ref.source_case_id,
            )

            if target_image_url is None:
                _store_match(db, SignatureMatch(
                    document_id=doc.id,
                    case_id=ref.source_case_id,
                    signature_reference_id=ref.id,
                    comparison_scope=ComparisonScope.in_case,
                    result=SignatureMatchResult.cannot_determine,
                    reasoning=(
                        "Signature comparison could not run for this document: "
                        "the target signature region could not be extracted from the file."
                    ),
                ))
                comparisons_stored += 1
                continue

            # A pixel-identical match needs no model opinion — and the model would
            # only call two identical images "consistent", which reads as reassurance.
            reuse = (
                find_signature_reuse(
                    storage,
                    reference_image_bytes=ref_image_bytes,
                    reference_pdf_bytes=ref_pdf_bytes,
                    reference_box=ref.bounding_box,
                    target_image_url=target_image_url,
                    target_pdf_url=doc.blob_storage_path,
                    target_box=target_bbox,
                )
                if ref_image_bytes
                else None
            )
            if reuse is not None:
                verdict, reuse_reasoning = reuse
                _store_match(db, SignatureMatch(
                    document_id=doc.id,
                    case_id=ref.source_case_id,
                    signature_reference_id=ref.id,
                    comparison_scope=ComparisonScope.in_case,
                    result=SignatureMatchResult(verdict),
                    reasoning=reuse_reasoning,
                ))
                comparisons_stored += 1
                reuse_found += 1
                continue

            target_data_uri = download_and_encode_signature(storage, target_image_url)
            if target_data_uri is None:
                _store_match(db, SignatureMatch(
                    document_id=doc.id,
                    case_id=ref.source_case_id,
                    signature_reference_id=ref.id,
                    comparison_scope=ComparisonScope.in_case,
                    result=SignatureMatchResult.cannot_determine,
                    reasoning=(
                        "Signature comparison could not run: target image download failed."
                    ),
                ))
                comparisons_stored += 1
                continue

            if llm_service is None:
                _store_match(db, SignatureMatch(
                    document_id=doc.id,
                    case_id=ref.source_case_id,
                    signature_reference_id=ref.id,
                    comparison_scope=ComparisonScope.in_case,
                    result=SignatureMatchResult.cannot_determine,
                    reasoning=(
                        "Signature comparison could not run: the vision model "
                        "service is not configured in this environment."
                    ),
                ))
                comparisons_stored += 1
                continue

            try:
                comparison = llm_service.compare_signatures(ref_data_uri, target_data_uri)
                result = SignatureMatchResult(comparison.result)
                reasoning = comparison.reasoning
            except (LLMOperationError, LLMConfigurationError, ValueError) as exc:
                result = SignatureMatchResult.cannot_determine
                reasoning = (
                    f"Vision model comparison could not complete: {str(exc)[:500]}"
                )

            _store_match(db, SignatureMatch(
                document_id=doc.id,
                case_id=ref.source_case_id,
                signature_reference_id=ref.id,
                comparison_scope=ComparisonScope.in_case,
                result=result,
                reasoning=reasoning,
            ))
            comparisons_stored += 1

        record_event(
            db,
            "signature_comparison_completed",
            case_id=ref.source_case_id,
            document_id=ref.source_document_id,
            event_data={
                "reference_id": str(ref.id),
                "person_name": ref.person_name,
                "is_library": ref.is_library,
                "comparisons_attempted": comparisons_attempted,
                "comparisons_stored": comparisons_stored,
                "reference_kind": reference_kind,
                "identical_reuse_found": reuse_found,
            },
        )
        db.commit()
        request_case_scoring(ref.source_case_id, company_uuid)

    finally:
        db.close()
