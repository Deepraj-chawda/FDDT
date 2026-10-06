"""
Unit tests for the OCR + classification + extraction Celery task
(app/tasks/document_processing.py). These mock the OCR/LLM/storage
service accessors — no live Azure Document Intelligence or Azure OpenAI
call happens here; that end-to-end behavior (including Arabic-language
accuracy) has to be verified against real sample documents, which this
suite deliberately doesn't attempt.

The task always opens its own `SessionLocal()` (it has no FastAPI request
context to get a session through) — these tests monkeypatch that name in
the task module to an in-memory-SQLite-backed sessionmaker, the same
StaticPool pattern tests/conftest.py's `db_session` fixture uses, so the
task's writes are visible to plain assertions afterward.
"""
import uuid
from unittest.mock import MagicMock

import pymupdf
import pytest

from tests.conftest import tenant_task_factory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.document_processing as document_processing_module
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.user import User, UserRole
from app.services.llm_service import (
    AdditionalField,
    AmountFieldValue,
    DateFieldValue,
    DocumentAnalysis,
    FieldValue,
    NumericFieldValue,
)
from app.services.ocr_service import OCRResult


def _empty_amount() -> AmountFieldValue:
    return AmountFieldValue(value=None, currency=None, raw_text=None, confidence=0.0, uncertain=True)


def _empty_numeric() -> NumericFieldValue:
    return NumericFieldValue(value=None, raw_text=None, confidence=0.0, uncertain=True)


@pytest.fixture()
def task_session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(
        engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON")
    )
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    monkeypatch.setattr(document_processing_module, "SessionLocal", factory)
    return factory


@pytest.fixture()
def seeded_document_id(task_session_factory) -> uuid.UUID:
    session = task_session_factory()
    try:
        user = User(
            email="uploader@example.com",
            hashed_password="not-a-real-hash",
            role=UserRole.user,
            is_active=True,
        )
        session.add(user)
        session.flush()

        case = Case(
            case_number="CASE-TESTDOC1",
            case_type=CaseType.vendor_invoice,
            submitted_by_user_id=user.id,
            status=CaseStatus.submitted,
        )
        session.add(case)
        session.flush()

        document = Document(
            case_id=case.id,
            uploaded_by_user_id=user.id,
            original_filename="test.pdf",
            blob_storage_path="https://fake.blob.core.windows.net/documents/test.pdf",
            file_hash="0" * 64,
            content_type="application/pdf",
            file_size_bytes=100,
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        return document.id
    finally:
        session.close()


def _one_page_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "INVOICE #1 Vendor: Acme Total: $10", fontname="helv", fontsize=10)
    return doc.tobytes()


def _patch_storage(monkeypatch, pdf_bytes: bytes | None = None):
    fake_storage = MagicMock()
    fake_storage.get_download_url.return_value = "https://fake.blob.core.windows.net/signed-url"
    fake_storage.download_bytes.return_value = pdf_bytes if pdf_bytes is not None else _one_page_pdf()
    monkeypatch.setattr(
        document_processing_module, "get_storage_service_for_task", lambda: fake_storage
    )
    return fake_storage


def test_process_document_success_stores_type_fields_and_ocr_text(
    task_session_factory, seeded_document_id, monkeypatch
):
    _patch_storage(monkeypatch)

    fake_ocr = MagicMock()
    fake_ocr.analyze_url.return_value = OCRResult(text="INVOICE #1 Vendor: Acme Total: $10")
    monkeypatch.setattr(document_processing_module, "get_ocr_service", lambda: fake_ocr)

    analysis = DocumentAnalysis(
        document_type="vendor_invoice",
        document_type_confidence=0.9,
        issuer=FieldValue(value="Acme", confidence=0.9, uncertain=False),
        reference_number=FieldValue(value="1", confidence=0.9, uncertain=False),
        date=DateFieldValue(value=None, raw_text=None, confidence=0.0, uncertain=True),
        amount=AmountFieldValue(
            value=10.0, currency="USD", raw_text="$10", confidence=0.9, uncertain=False
        ),
        subtotal=_empty_amount(),
        tax_amount=_empty_amount(),
        tax_rate=_empty_numeric(),
        additional_fields=[
            AdditionalField(field_name="vendor", value="Acme", confidence=0.9, uncertain=False)
        ],
    )
    fake_llm = MagicMock()
    fake_llm.classify_and_extract.return_value = analysis
    monkeypatch.setattr(document_processing_module, "get_llm_service", lambda: fake_llm)

    document_processing_module.process_document(str(seeded_document_id))

    session = task_session_factory()
    document = session.get(Document, seeded_document_id)
    assert document.processing_status == DocumentProcessingStatus.complete
    assert document.document_type == "vendor_invoice"
    assert document.processing_error is None
    assert document.ocr_text == "INVOICE #1 Vendor: Acme Total: $10"
    assert document.extracted_fields["core_fields"]["issuer"]["value"] == "Acme"
    assert document.extracted_fields["core_fields"]["date"]["uncertain"] is True
    assert document.extracted_fields["core_fields"]["amount"]["value"] == 10.0
    assert document.extracted_fields["core_fields"]["amount"]["currency"] == "USD"
    assert document.extracted_fields["additional_fields"][0]["field_name"] == "vendor"

    events = session.query(AuditLog).filter_by(document_id=seeded_document_id).all()
    assert {e.event_type for e in events} == {"document_processing_completed", "font_consistency_completed"}

    # The font consistency check runs here, on the document's own bytes.
    font = session.query(DocumentCheck).filter_by(
        document_id=seeded_document_id, check_type=DocumentCheckType.font_consistency
    ).one()
    assert font.status == DocumentCheckStatus.completed
    assert font.result["result"] == "pass"
    assert font.result["details"][0]["data"]["pages"] == [{"page": 1, "source": "text_layer"}]


def test_font_check_failure_never_fails_the_extraction(
    task_session_factory, seeded_document_id, monkeypatch
):
    _patch_storage(monkeypatch, pdf_bytes=b"not a pdf")
    fake_ocr = MagicMock()
    fake_ocr.analyze_url.return_value = OCRResult(text="text")
    monkeypatch.setattr(document_processing_module, "get_ocr_service", lambda: fake_ocr)
    fake_llm = MagicMock()
    fake_llm.classify_and_extract.return_value = DocumentAnalysis(
        document_type="vendor_invoice",
        document_type_confidence=0.9,
        issuer=FieldValue(value=None, confidence=0.0, uncertain=True),
        reference_number=FieldValue(value=None, confidence=0.0, uncertain=True),
        date=DateFieldValue(value=None, raw_text=None, confidence=0.0, uncertain=True),
        amount=_empty_amount(),
        subtotal=_empty_amount(),
        tax_amount=_empty_amount(),
        tax_rate=_empty_numeric(),
        additional_fields=[],
    )
    monkeypatch.setattr(document_processing_module, "get_llm_service", lambda: fake_llm)

    document_processing_module.process_document(str(seeded_document_id))

    session = task_session_factory()
    assert session.get(Document, seeded_document_id).processing_status == DocumentProcessingStatus.complete
    font = session.query(DocumentCheck).filter_by(
        document_id=seeded_document_id, check_type=DocumentCheckType.font_consistency
    ).one()
    assert font.status == DocumentCheckStatus.failed and font.error_message
    events = {e.event_type for e in session.query(AuditLog).filter_by(document_id=seeded_document_id)}
    assert events == {"document_processing_completed", "font_consistency_failed"}


def test_process_document_failure_marks_failed_with_reason(
    task_session_factory, seeded_document_id, monkeypatch
):
    _patch_storage(monkeypatch)

    fake_ocr = MagicMock()
    fake_ocr.analyze_url.side_effect = RuntimeError("simulated OCR outage")
    monkeypatch.setattr(document_processing_module, "get_ocr_service", lambda: fake_ocr)
    monkeypatch.setattr(document_processing_module, "get_llm_service", lambda: MagicMock())

    document_processing_module.process_document(str(seeded_document_id))

    session = task_session_factory()
    document = session.get(Document, seeded_document_id)
    assert document.processing_status == DocumentProcessingStatus.failed
    assert document.document_type is None
    assert "simulated OCR outage" in document.processing_error

    events = session.query(AuditLog).filter_by(document_id=seeded_document_id).all()
    assert {e.event_type for e in events} == {"document_processing_failed"}


def test_process_document_unknown_id_is_a_quiet_noop(task_session_factory):
    # Doesn't raise, doesn't write an audit_log row (there's no valid
    # document_id FK to attach one to).
    document_processing_module.process_document(str(uuid.uuid4()))

    session = task_session_factory()
    assert session.query(AuditLog).count() == 0
