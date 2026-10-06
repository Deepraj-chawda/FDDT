"""
Signature reference creation and comparison result retrieval.

All three routes are confined to the case's company (another company's case is a 404). They are
open to the case's owner (who sets a reference signature while uploading) and to the company's
reviewers; anyone else gets a 404. Platform admins may read the two lists (audited support access)
but not create references.

POST /{case_id}/documents/{document_id}/signature-references
  — Reviewer creates a reference from a drawn bounding box.
  — Crops and uploads the signature image, inserts the reference row,
    enqueues the in-case comparison Celery task immediately.
  — No automatic creation: only the explicit reviewer action triggers this.

GET /{case_id}/signature-references
  — Lists all references created for a case (for the upload screen to
    show what's been created during this session).

GET /{case_id}/signature-matches
  — Lists all in-case match results for a case, enriched with the
    reference person_name. Used by the case detail Checks panel.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_company_role
from app.api.case_access import load_visible_case
from app.api.tenant_access import CaseScope, get_case_scope
from app.models.document import Document
from app.models.signature_match import ComparisonScope, SignatureMatch
from app.models.signature_reference import SignatureReference
from app.models.user import User, UserRole
from app.schemas.signature import (
    SignatureMatchResponse,
    SignatureReferenceCreate,
    SignatureReferenceResponse,
)
from app.services.audit_service import record_event
from app.services.signature_comparison_service import crop_and_upload_signature
from app.services.storage_service import StorageOperationError, StorageService, get_storage_service
from app.tasks.signature_comparison_task import run_signature_comparison

router = APIRouter(prefix="/cases", tags=["signatures"])


@router.post(
    "/{case_id}/documents/{document_id}/signature-references",
    response_model=SignatureReferenceResponse,
    status_code=201,
    summary="Create a signature reference",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Platform admins have read-only access."},
        404: {"description": "No such case (or, for a `user`, not one they submitted), or the document is not in this case."},
    },
)
def create_signature_reference(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: SignatureReferenceCreate,
    current_user: User = Depends(require_company_role(UserRole.user)),
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> SignatureReferenceResponse:
    """Create a signature reference from a reviewer-drawn bounding box.

    Crops the signature region from the document's PDF, uploads it to
    Blob Storage, inserts the signature_references row, then immediately
    enqueues the in-case comparison Celery task for this reference.

    `person_name` is required and must be typed by the reviewer — the
    system does not attempt to auto-extract it (System Specification §2.3).
    """
    # The uploader sets reference signatures during upload, so the case owner is allowed as well as
    # reviewers; anyone else gets a 404 (same rule as GET /cases/{id}).
    db = scope.db
    load_visible_case(db, case_id, current_user)

    document = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.case_id == case_id,
            Document.company_id == scope.company_id,
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found in this case",
        )

    reference_id = uuid.uuid4()
    bounding_box_dict = payload.bounding_box.model_dump()

    # Crop the signature region from the raw PDF and upload to Blob Storage.
    # This is best-effort — if the crop fails (e.g. document is not a PDF,
    # rendering fails), we still create the reference row with a null URL
    # rather than blocking the reviewer entirely. The comparison task will
    # record a cannot_determine result for each target in that case.
    try:
        signature_image_url = crop_and_upload_signature(
            storage,
            document.blob_storage_path,
            bounding_box_dict,
            reference_id=reference_id,
            company_id=scope.company_id,
            case_id=case_id,
        )
    except (StorageOperationError, Exception):  # noqa: BLE001
        signature_image_url = None

    ref = SignatureReference(
        id=reference_id,
        company_id=scope.company_id,
        person_name=payload.person_name,
        signature_image_url=signature_image_url,
        bounding_box=bounding_box_dict,
        source_document_id=document_id,
        source_case_id=case_id,
        created_by=current_user.id,
        is_library=payload.is_library,
    )
    db.add(ref)
    db.flush()

    record_event(
        db,
        "signature_reference_created",
        case_id=case_id,
        document_id=document_id,
        actor_user_id=current_user.id,
        event_data={
            "reference_id": str(reference_id),
            "person_name": payload.person_name,
            "is_library": payload.is_library,
            "bounding_box": bounding_box_dict,
            "image_captured": signature_image_url is not None,
        },
    )
    db.commit()
    db.refresh(ref)

    # Enqueue in-case comparison immediately after committing the reference row.
    # Library references (is_library=True) run through the same in-case comparison
    # — the flag only affects future library-wide logic, not this.
    run_signature_comparison.delay(str(reference_id), str(scope.company_id))

    return SignatureReferenceResponse.from_ref(ref)


@router.get(
    "/{case_id}/signature-references",
    response_model=list[SignatureReferenceResponse],
    summary="List a case's signature references",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "No such case (or, for a `user`, not one they submitted)."},
    },
)
def list_signature_references(
    case_id: uuid.UUID,
    scope: CaseScope = Depends(get_case_scope),
) -> list[SignatureReferenceResponse]:
    """All signature references created for this case. Used by the upload
    screen to list what references were set during this session."""
    # The uploader sets reference signatures during upload, so the case owner is allowed as well as
    # reviewers/admins; anyone else gets a 404 (same rule as GET /cases/{id}).
    db = scope.db
    load_visible_case(db, case_id, scope.ctx.user)

    refs = db.execute(
        select(SignatureReference)
        .where(
            SignatureReference.source_case_id == case_id,
            SignatureReference.company_id == scope.company_id,
        )
        .order_by(SignatureReference.created_at.asc())
    ).scalars().all()

    return [SignatureReferenceResponse.from_ref(r) for r in refs]


@router.get(
    "/{case_id}/signature-matches",
    response_model=list[SignatureMatchResponse],
    summary="List a case's signature comparison results",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "No such case (or, for a `user`, not one they submitted)."},
    },
)
def list_signature_matches(
    case_id: uuid.UUID,
    scope: CaseScope = Depends(get_case_scope),
) -> list[SignatureMatchResponse]:
    """All in-case signature match results for this case, enriched with the
    reference person_name. Filtered to comparison_scope='in_case' only —
    library-scope matches are not generated yet (System Specification §2.3)."""
    # The uploader sets reference signatures during upload, so the case owner is allowed as well as
    # reviewers/admins; anyone else gets a 404 (same rule as GET /cases/{id}).
    db = scope.db
    load_visible_case(db, case_id, scope.ctx.user)

    matches = db.execute(
        select(SignatureMatch)
        .where(
            SignatureMatch.case_id == case_id,
            SignatureMatch.company_id == scope.company_id,
            SignatureMatch.comparison_scope == ComparisonScope.in_case,
        )
        .order_by(SignatureMatch.compared_at.asc())
    ).scalars().all()

    # Batch-load referenced rows to avoid N+1 per match.
    ref_ids = {m.signature_reference_id for m in matches}
    refs_by_id: dict[uuid.UUID, SignatureReference] = {}
    if ref_ids:
        ref_rows = db.execute(
            select(SignatureReference).where(
                SignatureReference.id.in_(ref_ids), SignatureReference.company_id == scope.company_id
            )
        ).scalars().all()
        refs_by_id = {r.id: r for r in ref_rows}

    return [
        SignatureMatchResponse.from_match(
            m,
            reference_person_name=refs_by_id[m.signature_reference_id].person_name
            if m.signature_reference_id in refs_by_id
            else "Unknown",
        )
        for m in matches
    ]
