import uuid
from unittest.mock import MagicMock
import pytest

from tests.conftest import tenant_task_factory

from app.models.signature_reference import SignatureReference
from app.models.signature_match import SignatureMatch, ComparisonScope, SignatureMatchResult
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.tasks.signature_comparison_task import run_signature_comparison, _get_detected_signature_bbox

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.tasks.signature_comparison_task as signature_task_module
from app.models import Base
from app.models.user import User, UserRole

@pytest.fixture()
def signature_task_session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    monkeypatch.setattr(signature_task_module, "SessionLocal", factory)
    return factory

def test_signature_comparison_task_no_ref(signature_task_session_factory):
    # Running for nonexistent reference id should not raise
    run_signature_comparison(str(uuid.uuid4()))

def test_signature_comparison_task_flow(monkeypatch, signature_task_session_factory):
    from app.models.case import Case, CaseStatus, CaseType
    from app.models.document import Document

    session = signature_task_session_factory()
    try:
        user = User(email="test.signer@example.com", hashed_password="x", role=UserRole.reviewer_l2, is_active=True)
        session.add(user)
        session.flush()

        case_id = uuid.uuid4()
        case = Case(
            id=case_id,
            case_number=f"CASE-{case_id.hex[:8].upper()}",
            case_type=CaseType.vendor_invoice,
            submitted_by_user_id=user.id,
            status=CaseStatus.submitted,
        )
        session.add(case)

        # Document 1: invoice (reference source)
        doc_ref = Document(
            id=uuid.uuid4(),
            case_id=case_id,
            uploaded_by_user_id=user.id,
            original_filename="invoice.pdf",
            content_type="application/pdf",
            file_size_bytes=100,
            file_hash="hash_ref",
            blob_storage_path="invoice.pdf",
        )
        session.add(doc_ref)

        # Document 2: evidence (target with detected signature)
        doc_target = Document(
            id=uuid.uuid4(),
            case_id=case_id,
            uploaded_by_user_id=user.id,
            original_filename="evidence.pdf",
            content_type="application/pdf",
            file_size_bytes=100,
            file_hash="hash_target",
            blob_storage_path="evidence.pdf",
        )
        session.add(doc_target)

        # Document 3: another doc with NO detected signature
        doc_no_sig = Document(
            id=uuid.uuid4(),
            case_id=case_id,
            uploaded_by_user_id=user.id,
            original_filename="no_sig.pdf",
            content_type="application/pdf",
            file_size_bytes=100,
            file_hash="hash_no_sig",
            blob_storage_path="no_sig.pdf",
        )
        session.add(doc_no_sig)

        # Add detection check on doc_target
        sig_check = DocumentCheck(
            document_id=doc_target.id,
            check_type=DocumentCheckType.signature_stamp_detection,
            status=DocumentCheckStatus.completed,
            result={
                "result": "pass",
                "details": {
                    "bounding_box": {"page": 1, "x": 0.5, "y": 0.5, "width": 0.2, "height": 0.1}
                }
            }
        )
        session.add(sig_check)

        # Create reference for doc_ref
        ref = SignatureReference(
            id=uuid.uuid4(),
            person_name="Authorized Signer",
            source_case_id=case_id,
            source_document_id=doc_ref.id,
            created_by=user.id,
            signature_image_url="https://fake.blob/signatures/ref.png",
            bounding_box={"page": 1, "x": 0.2, "y": 0.2, "width": 0.2, "height": 0.1},
            is_library=False,
        )
        session.add(ref)
        session.commit()
        ref_id = ref.id
        doc_target_id = doc_target.id
    finally:
        session.close()

    # Mock crop and download functions
    monkeypatch.setattr(
        signature_task_module,
        "download_and_encode_signature",
        lambda storage, url: "data:image/png;base64,fakeimage"
    )
    monkeypatch.setattr(
        signature_task_module,
        "crop_and_upload_signature",
        lambda storage, blob_path, bbox, reference_id, **_scope: "https://fake.blob/signatures/target.png"
    )

    # Mock LLM service
    mock_llm = MagicMock()
    from app.services.llm_service import SignatureComparisonResult
    mock_llm.compare_signatures.return_value = SignatureComparisonResult(
        result="consistent",
        reasoning="Visually consistent with reference on file; stroke curves match."
    )
    monkeypatch.setattr(signature_task_module, "get_llm_service", lambda: mock_llm)

    # Run comparison task
    run_signature_comparison(str(ref_id))

    # Verify signature_matches in DB with fresh session
    verify_session = signature_task_session_factory()
    try:
        matches = verify_session.query(SignatureMatch).filter(SignatureMatch.case_id == case_id).all()
        # Exactly 1 match (against doc_target; doc_ref is excluded because it's source, doc_no_sig has no detection)
        assert len(matches) == 1
        m = matches[0]
        assert m.document_id == doc_target_id
        assert m.signature_reference_id == ref_id
        assert m.comparison_scope == ComparisonScope.in_case
        assert m.result == SignatureMatchResult.consistent
        assert "Visually consistent" in m.reasoning
    finally:
        verify_session.close()

    # Idempotent: the task re-runs whenever another document's detection
    # finishes, and must not duplicate an already-stored comparison.
    run_signature_comparison(str(ref_id))
    verify_session = signature_task_session_factory()
    try:
        assert verify_session.query(SignatureMatch).filter(SignatureMatch.case_id == case_id).count() == 1
        assert mock_llm.compare_signatures.call_count == 1
    finally:
        verify_session.close()
