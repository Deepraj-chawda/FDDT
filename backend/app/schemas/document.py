import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.services.check_summaries import summarize_document_checks
from app.services.field_exception_service import with_field_regions


class DocumentResponse(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    original_filename: str
    content_type: str | None
    file_size_bytes: int | None
    file_hash: str
    # A short-lived signed URL when the caller passes one (the upload
    # endpoint does — the storage container is private, so the durable URL
    # alone isn't browser-fetchable, see app/services/storage_service.py);
    # otherwise the durable Azure Blob Storage URL stored in the
    # `blob_storage_path` column per the schema in System Specification section 5.
    file_url: str
    uploaded_at: datetime
    processing_status: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_document(cls, document, file_url: str | None = None) -> "DocumentResponse":
        return cls(
            id=document.id,
            case_id=document.case_id,
            original_filename=document.original_filename,
            content_type=document.content_type,
            file_size_bytes=document.file_size_bytes,
            file_hash=document.file_hash,
            file_url=file_url or document.blob_storage_path,
            uploaded_at=document.created_at,
            processing_status=document.processing_status.value,
        )


class DocumentFileUrlResponse(BaseModel):
    """A fresh short-lived signed URL for one document's original file."""

    file_url: str


class DocumentCheckSummary(BaseModel):
    """One `document_checks` row (System Specification sections 2.2/3.1/3.2 —
    field_validation, issuer_verification, and later the forensic checks).
    `result` holds whatever shape that check_type produces — see the
    individual check services (e.g. app/services/field_validation_service.py)
    for what's inside — the frontend's Checks panel renders it generically
    rather than assuming a fixed schema per check_type."""

    id: uuid.UUID
    check_type: str
    status: str
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    # Short, readable form of the result (headline, one-line items, notes) —
    # app/services/check_summaries.py. Computed when read.
    summary: dict[str, Any] | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_check(
        cls, check, extracted_fields: dict[str, Any] | None = None, summary: dict[str, Any] | None = None
    ) -> "DocumentCheckSummary":
        result = check.result
        if check.check_type.value == "field_validation":
            # Rows stored before field locations existed carry no highlight
            # regions; derive them (read-time only) from the document's
            # now-located fields.
            result = with_field_regions(result, extracted_fields)
        return cls(
            id=check.id,
            check_type=check.check_type.value,
            status=check.status.value,
            result=result,
            error_message=check.error_message,
            created_at=check.created_at,
            summary=summary,
        )


class CaseDocumentSummary(BaseModel):
    """A document as shown on the case-detail page. `file_url` here is a
    short-lived signed URL (see StorageService.get_download_url) — the
    storage container is private, so the durable URL alone isn't
    browser-fetchable.

    `document_type` and `extracted_fields` are filled in by the OCR +
    classification + extraction Celery pipeline (app/tasks/document_
    processing.py) — both stay null until `processing_status` reaches
    "complete". `extracted_fields` shape: {"document_type_confidence":
    float, "core_fields": {name: {value, confidence, uncertain}},
    "additional_fields": [{field_name, value, confidence, uncertain}]}.

    `checks` is filled in afterward by app/tasks/document_checks.py
    (field_validation, issuer_verification, and later the forensic
    checks) — empty until those complete."""

    id: uuid.UUID
    original_filename: str
    document_type: str | None
    processing_status: str
    extracted_fields: dict[str, Any] | None
    processing_error: str | None
    content_type: str | None
    file_size_bytes: int | None
    file_hash: str
    file_url: str
    uploaded_at: datetime
    checks: list[DocumentCheckSummary]

    @classmethod
    def from_document(cls, document, file_url: str) -> "CaseDocumentSummary":
        summaries = summarize_document_checks(document.checks)
        return cls(
            id=document.id,
            original_filename=document.original_filename,
            document_type=document.document_type,
            processing_status=document.processing_status.value,
            extracted_fields=document.extracted_fields,
            processing_error=document.processing_error,
            content_type=document.content_type,
            file_size_bytes=document.file_size_bytes,
            file_hash=document.file_hash,
            file_url=file_url,
            uploaded_at=document.created_at,
            checks=[
                DocumentCheckSummary.from_check(
                    check, document.extracted_fields, summaries.get(check.check_type.value)
                )
                for check in sorted(document.checks, key=lambda c: c.created_at)
            ],
        )
