"""
Unit tests for the field-validation + issuer-verification Celery task
and the case-level cross-document-consistency task
(app/tasks/document_checks.py). Same in-memory-SQLite pattern as
tests/test_document_processing.py — these tests monkeypatch the task
module's `SessionLocal` and call the task functions directly (bypassing
`.delay()`/a real broker).

`run_document_checks` calls `get_llm_service()` for the issuer-
verification LLM fallback (app/services/issuer_service.py). This
environment has real Azure OpenAI credentials configured, so every test
here patches `get_llm_service` to a stub — otherwise these "unit" tests
would make real network calls whenever a fuzzy match doesn't clear the
threshold, same as tests/test_document_processing.py already does for
the OCR/LLM extraction call.
"""
import uuid
from unittest.mock import MagicMock

import pytest

from tests.conftest import tenant_task_factory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.document_checks as document_checks_module
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.cross_document_finding import CrossDocumentFinding
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.models.user import User, UserRole
from app.services.llm_service import EntityMatchJudgment


@pytest.fixture(autouse=True)
def _stub_llm_service(monkeypatch):
    """No test in this file exercises a real LLM call — see module
    docstring. Returns "no match" by default; a test that wants the LLM
    fallback to actually match something overrides `judge_entity_match`
    on the mock after fetching it via `document_checks_module.
    get_llm_service()`."""
    fake_llm = MagicMock()
    fake_llm.judge_entity_match.return_value = EntityMatchJudgment(
        matched_candidate=None, reasoning="stubbed: no match"
    )
    monkeypatch.setattr(document_checks_module, "get_llm_service", lambda: fake_llm)
    return fake_llm


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
    monkeypatch.setattr(document_checks_module, "SessionLocal", factory)
    return factory


def _extracted_fields(
    *, issuer=None, date=None, amount=None, currency="USD", subtotal=None, tax_amount=None, reference_number=None
):
    def _str_field(value):
        return {"value": value, "confidence": 0.9, "uncertain": False}

    def _date_field(value):
        return {"value": value, "raw_text": value, "confidence": 0.9, "uncertain": False}

    def _amount_field(value):
        return {
            "value": value,
            "currency": currency if value is not None else None,
            "raw_text": str(value) if value is not None else None,
            "confidence": 0.9,
            "uncertain": False,
        }

    return {
        "document_type_confidence": 0.9,
        "core_fields": {
            "issuer": _str_field(issuer),
            "date": _date_field(date),
            "amount": _amount_field(amount),
            "subtotal": _amount_field(subtotal),
            "tax_amount": _amount_field(tax_amount),
            "tax_rate": {"value": None, "raw_text": None, "confidence": 0.0, "uncertain": True},
            "reference_number": _str_field(reference_number),
        },
        "additional_fields": [],
    }


def _seed_case_with_documents(session_factory, *, document_count: int):
    session = session_factory()
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
            case_number="CASE-TESTCHK1",
            case_type=CaseType.vendor_invoice,
            submitted_by_user_id=user.id,
            status=CaseStatus.submitted,
        )
        session.add(case)
        session.flush()

        document_ids = []
        for i in range(document_count):
            document = Document(
                case_id=case.id,
                uploaded_by_user_id=user.id,
                original_filename=f"doc{i}.pdf",
                blob_storage_path=f"https://fake.blob.core.windows.net/documents/doc{i}.pdf",
                file_hash=str(i) * 64,
                processing_status=DocumentProcessingStatus.pending,
            )
            session.add(document)
            session.flush()
            document_ids.append(document.id)

        session.commit()
        return case.id, document_ids
    finally:
        session.close()


def _mark_complete(session_factory, document_id, extracted_fields):
    session = session_factory()
    try:
        document = session.get(Document, document_id)
        document.processing_status = DocumentProcessingStatus.complete
        document.extracted_fields = extracted_fields
        session.commit()
    finally:
        session.close()


def _mark_failed(session_factory, document_id):
    session = session_factory()
    try:
        document = session.get(Document, document_id)
        document.processing_status = DocumentProcessingStatus.failed
        document.processing_error = "simulated failure"
        session.commit()
    finally:
        session.close()


def _seed_registry_entry(session_factory, name: str):
    session = session_factory()
    try:
        session.add(IssuerRegistry(name=name, type=IssuerType.vendor))
        session.commit()
    finally:
        session.close()


class TestRunDocumentChecks:
    def test_writes_field_validation_and_issuer_verification_checks(
        self, task_session_factory, monkeypatch
    ):
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        _seed_registry_entry(task_session_factory, "Acme LLC")
        _mark_complete(
            task_session_factory,
            document_id,
            _extracted_fields(issuer="Acme LLC", date="2020-01-01", amount=100.0, reference_number="INV-1"),
        )
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        checks = session.query(DocumentCheck).filter_by(document_id=document_id).all()
        by_type = {c.check_type: c for c in checks}
        assert by_type[DocumentCheckType.field_validation].status == DocumentCheckStatus.completed
        assert by_type[DocumentCheckType.field_validation].result["result"] == "pass"
        assert by_type[DocumentCheckType.issuer_verification].result["result"] == "pass"
        assert by_type[DocumentCheckType.issuer_verification].result["details"]["matched_registry_name"] == "Acme LLC"
        assert by_type[DocumentCheckType.issuer_verification].result["details"]["matched_via"] == "fuzzy"

        events = session.query(AuditLog).filter_by(document_id=document_id).all()
        assert {e.event_type for e in events} == {"document_checks_completed"}

    def test_issuer_with_an_empty_registry_is_not_checked(self, task_session_factory, monkeypatch):
        # Nothing to compare against is not evidence of anything (rule
        # issuer.not_in_registry v2).
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        _mark_complete(
            task_session_factory, document_id, _extracted_fields(issuer="Totally Unknown Co")
        )
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        check = (
            session.query(DocumentCheck)
            .filter_by(document_id=document_id, check_type=DocumentCheckType.issuer_verification)
            .one()
        )
        assert check.result["result"] == "not_checked"
        assert "registry has no active entries" in check.result["details"]["reason"]

    def test_issuer_not_in_registry_is_flagged(self, task_session_factory, monkeypatch):
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        _seed_registry_entry(task_session_factory, "Meridian Industrial Supplies LLC")
        _mark_complete(
            task_session_factory, document_id, _extracted_fields(issuer="Totally Unknown Co")
        )
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        check = (
            session.query(DocumentCheck)
            .filter_by(document_id=document_id, check_type=DocumentCheckType.issuer_verification)
            .one()
        )
        assert check.result["result"] == "flag"
        assert check.result["details"]["matched_via"] is None

    def test_issuer_matched_via_llm_fallback(self, task_session_factory, monkeypatch, _stub_llm_service):
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        _seed_registry_entry(task_session_factory, "Al Wadi Al Akhdar General Trading LLC")
        _mark_complete(
            task_session_factory,
            document_id,
            _extracted_fields(issuer="شركة الوادي الأخضر للتجارة العامة ذ.م.م"),
        )
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())
        _stub_llm_service.judge_entity_match.return_value = EntityMatchJudgment(
            matched_candidate="Al Wadi Al Akhdar General Trading LLC", reasoning="transliteration match"
        )

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        check = (
            session.query(DocumentCheck)
            .filter_by(document_id=document_id, check_type=DocumentCheckType.issuer_verification)
            .one()
        )
        assert check.result["result"] == "pass"
        assert check.result["details"]["matched_via"] == "llm"

    def test_future_date_is_flagged_in_field_validation(self, task_session_factory, monkeypatch):
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        _mark_complete(task_session_factory, document_id, _extracted_fields(date="2099-01-01"))
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        check = (
            session.query(DocumentCheck)
            .filter_by(document_id=document_id, check_type=DocumentCheckType.field_validation)
            .one()
        )
        assert check.result["result"] == "flag"
        assert check.result["details"]["date_in_future"]["status"] == "flag"

    def test_noop_when_document_not_complete(self, task_session_factory, monkeypatch):
        case_id, [document_id] = _seed_case_with_documents(task_session_factory, document_count=1)
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", MagicMock())

        document_checks_module.run_document_checks(str(document_id))

        session = task_session_factory()
        assert session.query(DocumentCheck).count() == 0

    def test_noop_for_unknown_document_id(self, task_session_factory):
        # Doesn't raise.
        document_checks_module.run_document_checks(str(uuid.uuid4()))

    def test_enqueues_cross_document_check_once_all_documents_terminal(
        self, task_session_factory, monkeypatch
    ):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0))
        # doc2 still pending — completing doc1's checks shouldn't enqueue yet.
        fake_delay = MagicMock()
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", fake_delay)

        document_checks_module.run_document_checks(str(doc1))
        fake_delay.assert_not_called()

        _mark_complete(task_session_factory, doc2, _extracted_fields(amount=100.0))
        document_checks_module.run_document_checks(str(doc2))
        # The follow-up task carries the company so its worker session is
        # confined to it.
        fake_delay.assert_called_once_with(str(case_id), str(task_session_factory.company_id))

    def test_failed_document_still_counts_toward_case_completion(
        self, task_session_factory, monkeypatch
    ):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0))
        _mark_failed(task_session_factory, doc2)
        fake_delay = MagicMock()
        monkeypatch.setattr(document_checks_module.run_cross_document_checks, "delay", fake_delay)

        # Simulates document_processing.py's failure path calling this
        # directly (doc2 never reaches run_document_checks since
        # extraction itself failed for it).
        session = task_session_factory()
        try:
            document_checks_module._maybe_enqueue_cross_document_check(
                session, task_session_factory.company_id, case_id
            )
        finally:
            session.close()

        fake_delay.assert_called_once_with(str(case_id), str(task_session_factory.company_id))


class TestRunCrossDocumentChecks:
    def test_writes_findings_for_mismatched_documents(self, task_session_factory):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0))
        _mark_complete(task_session_factory, doc2, _extracted_fields(amount=999.0))

        document_checks_module.run_cross_document_checks(str(case_id))

        session = task_session_factory()
        findings = session.query(CrossDocumentFinding).filter_by(case_id=case_id).all()
        assert len(findings) == 1
        assert findings[0].field_name == "amount"
        assert findings[0].finding_type == "cross_document_consistency"

        events = session.query(AuditLog).filter_by(case_id=case_id).all()
        assert any(e.event_type == "cross_document_check_completed" for e in events)

    def test_no_findings_when_consistent(self, task_session_factory):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0, issuer="Acme"))
        _mark_complete(task_session_factory, doc2, _extracted_fields(amount=100.0, issuer="Acme"))

        document_checks_module.run_cross_document_checks(str(case_id))

        session = task_session_factory()
        assert session.query(CrossDocumentFinding).filter_by(case_id=case_id).count() == 0

    def test_rerun_clears_previous_findings(self, task_session_factory):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0))
        _mark_complete(task_session_factory, doc2, _extracted_fields(amount=999.0))
        document_checks_module.run_cross_document_checks(str(case_id))

        # Now the documents agree — rerunning should leave no stale finding.
        _mark_complete(task_session_factory, doc2, _extracted_fields(amount=100.0))
        document_checks_module.run_cross_document_checks(str(case_id))

        session = task_session_factory()
        assert session.query(CrossDocumentFinding).filter_by(case_id=case_id).count() == 0

    def test_fewer_than_two_completed_documents_writes_no_findings(self, task_session_factory):
        case_id, [doc1, doc2] = _seed_case_with_documents(task_session_factory, document_count=2)
        _mark_complete(task_session_factory, doc1, _extracted_fields(amount=100.0))
        _mark_failed(task_session_factory, doc2)

        document_checks_module.run_cross_document_checks(str(case_id))

        session = task_session_factory()
        assert session.query(CrossDocumentFinding).filter_by(case_id=case_id).count() == 0
