"""
Pydantic schemas for the signature verification API (app/api/signatures.py).

These mirror the signature_references and signature_matches ORM models
(app/models/signature_reference.py / signature_match.py).
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Bounding box (shared with detection and overlay rendering)
# ---------------------------------------------------------------------------

class BoundingBoxSchema(BaseModel):
    """Normalized bounding box — page-fraction (0-1) coordinates, same
    convention as every other bounding box in this codebase (ELA, copy-move,
    visual inconsistency). `page` is 1-based."""

    page: int = Field(..., ge=1)
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    width: float = Field(..., gt=0.0, le=1.0)
    height: float = Field(..., gt=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Reference creation
# ---------------------------------------------------------------------------

class SignatureReferenceCreate(BaseModel):
    """Request body for POST /cases/{case_id}/documents/{document_id}/
    signature-references.

    `person_name` is required and must be provided by the reviewer —
    the system never attempts to auto-extract or pre-fill it (System Specification §2.3).
    """

    person_name: str = Field(..., min_length=1, description=(
        "Name of the person whose signature this is, typed by the reviewer. "
        "Required — the system does not attempt to auto-extract this field."
    ))
    bounding_box: BoundingBoxSchema
    is_library: bool = Field(
        default=True,
        description=(
            "Defaults to True: every reference is saved to the permanent "
            "signature library. False is still accepted (case-only reference). "
            "Either way, at this phase a reference is only compared against "
            "other documents in its own case — no cross-case/library-wide "
            "comparison logic runs yet."
        ),
    )


class SignatureReferenceResponse(BaseModel):
    """One signature_references row returned by the API."""

    id: uuid.UUID
    person_name: str
    signature_image_url: str | None
    bounding_box: dict[str, Any] | None
    source_document_id: uuid.UUID
    source_case_id: uuid.UUID
    created_by: uuid.UUID
    is_library: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_ref(cls, ref) -> "SignatureReferenceResponse":
        return cls(
            id=ref.id,
            person_name=ref.person_name,
            signature_image_url=ref.signature_image_url,
            bounding_box=ref.bounding_box,
            source_document_id=ref.source_document_id,
            source_case_id=ref.source_case_id,
            created_by=ref.created_by,
            is_library=ref.is_library,
            created_at=ref.created_at,
        )


# ---------------------------------------------------------------------------
# Match results
# ---------------------------------------------------------------------------

# Human-readable labels for the four result values — advisory language only
# per System Specification §2.3/§4. No "verified", "confirmed", or "authenticated".
SIGNATURE_MATCH_RESULT_LABELS: dict[str, str] = {
    "consistent": "Visually consistent with reference on file",
    "possibly_consistent": "Possibly consistent with reference on file",
    "inconsistent": "Visual appearance inconsistent with reference",
    "cannot_determine": "Cannot determine — insufficient image quality",
    "identical_reuse": "Pixel-identical to reference — same image appears to be reused",
    "reused_different_signer": "Pixel-identical to reference, but printed signer differs",
}


class SignatureMatchResponse(BaseModel):
    """One signature_matches row returned by the API, enriched with the
    reference's person_name so the frontend can show "compared against [name]"
    without a separate reference lookup."""

    id: uuid.UUID
    document_id: uuid.UUID
    case_id: uuid.UUID
    signature_reference_id: uuid.UUID
    # person_name from the joined signature_references row
    reference_person_name: str
    comparison_scope: str
    result: str
    result_label: str
    reasoning: str | None
    compared_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_match(cls, match, reference_person_name: str) -> "SignatureMatchResponse":
        result_value = match.result.value if hasattr(match.result, "value") else str(match.result)
        return cls(
            id=match.id,
            document_id=match.document_id,
            case_id=match.case_id,
            signature_reference_id=match.signature_reference_id,
            reference_person_name=reference_person_name,
            comparison_scope=(
                match.comparison_scope.value
                if hasattr(match.comparison_scope, "value")
                else str(match.comparison_scope)
            ),
            result=result_value,
            result_label=SIGNATURE_MATCH_RESULT_LABELS.get(result_value, result_value),
            reasoning=match.reasoning,
            compared_at=match.compared_at,
        )
