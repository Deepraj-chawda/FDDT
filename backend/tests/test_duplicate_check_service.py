"""
Unit tests for app/services/forensics/duplicate_check.py. Unlike
ela.py/copy_move.py, this check needs a DB session directly (to read
prior documents' stored page hashes and to persist this document's own —
see app/services/issuer_service.py for the same db-session-in-a-service
pattern), so these tests use the shared `db_session` fixture (in-memory
SQLite, see tests/conftest.py) rather than being pure-function tests.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_page_hash import DocumentPageHash
from app.models.user import User, UserRole
from app.services.forensics.duplicate_check import run_duplicate_check
from app.services.forensics.pdf_render import RenderedPage

_WIDTH, _HEIGHT = 900, 1200


def _textured_page(rng_seed: int = 0, *, page_number: int = 1) -> RenderedPage:
    """Same synthetic rendered-text-page generator as
    tests/test_copy_move_service.py — real enough texture that a
    perceptual hash actually varies with the seed, unlike a flat fill."""
    rng = np.random.default_rng(rng_seed)
    image = np.full((_HEIGHT, _WIDTH, 3), 245, dtype=np.uint8)
    words = ["Invoice", "Total", "Amount", "Due", "Reference", "Vendor", "Tax", "Subtotal"]
    for _ in range(150):
        x, y = int(rng.integers(20, _WIDTH - 160)), int(rng.integers(20, _HEIGHT - 20))
        text = f"{words[rng.integers(0, len(words))]} {rng.integers(1000, 9999)}"
        cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
    return RenderedPage(page_number=page_number, image=image, width=_WIDTH, height=_HEIGHT, has_image_content=True)


def _near_duplicate(page: RenderedPage, *, vendor_text: str, page_number: int = 1) -> RenderedPage:
    """A copy of `page` with a small text overlay (simulating a
    near-matching-but-not-identical vendor name on a re-submitted
    quotation) and a JPEG re-save — the kind of file-level difference
    that would defeat a byte/SHA-256 comparison but not a perceptual
    hash."""
    edited = page.image.copy()
    cv2.putText(edited, vendor_text, (15, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", edited, [cv2.IMWRITE_JPEG_QUALITY, 95])
    resaved = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return RenderedPage(page_number=page_number, image=resaved, width=page.width, height=page.height, has_image_content=True)


@pytest.fixture()
def seeded_case(db_session):
    user = User(email="uploader@example.com", hashed_password="x", role=UserRole.user, is_active=True)
    db_session.add(user)
    db_session.flush()
    case = Case(
        case_number="CASE-DUPCHECK",
        case_type=CaseType.vendor_invoice,
        submitted_by_user_id=user.id,
        status=CaseStatus.submitted,
    )
    db_session.add(case)
    db_session.flush()
    return case, user


def _make_document(db_session, case, user, *, filename="doc.pdf", file_hash="0" * 64) -> Document:
    document = Document(
        case_id=case.id,
        uploaded_by_user_id=user.id,
        original_filename=filename,
        blob_storage_path=f"https://fake.blob/{filename}",
        file_hash=file_hash,
        content_type="application/pdf",
    )
    db_session.add(document)
    db_session.flush()
    return document


def test_first_upload_always_passes_and_stores_its_own_hashes(db_session, seeded_case):
    case, user = seeded_case
    document = _make_document(db_session, case, user)

    result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=document.id, pages=[_textured_page(0)])

    assert result == {"result": "pass", "details": []}
    stored = db_session.query(DocumentPageHash).filter_by(document_id=document.id).all()
    assert len(stored) == 1
    assert stored[0].page_number == 1


def test_near_identical_resubmission_flags_against_prior_document(db_session, seeded_case):
    """Stand-in for the near-duplicate-quotations scenario (this repo's
    sample-documents/ has no Sample5/quotation pair to test against
    directly — see the task write-up): the second document is a
    near-identical copy of the first (small vendor-name text overlay +
    JPEG re-save), same as two near-duplicate quotations with
    near-matching vendor names would look once rendered to an image."""
    case, user = seeded_case
    first_document = _make_document(db_session, case, user, filename="quotation_v1.pdf")
    base_page = _textured_page(0)
    first_result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=first_document.id, pages=[base_page])
    assert first_result["result"] == "pass"

    second_document = _make_document(db_session, case, user, filename="quotation_v2.pdf")
    near_duplicate_page = _near_duplicate(base_page, vendor_text="Al Falah Genral Trading LLC")
    second_result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=second_document.id, pages=[near_duplicate_page])

    assert second_result["result"] == "flag"
    assert len(second_result["details"]) == 1
    finding = second_result["details"][0]
    assert finding["finding"] == "near_duplicate_page"
    assert finding["severity"] == "high"
    assert finding["page"] == 1
    assert finding["data"]["matched_document_id"] == str(first_document.id)
    assert finding["data"]["matched_document_filename"] == "quotation_v1.pdf"
    assert finding["data"]["matched_case_id"] == str(case.id)
    assert finding["data"]["matched_case_number"] == case.case_number
    assert finding["data"]["matched_page"] == 1
    assert finding["data"]["distance"] <= 5
    assert "quotation_v1.pdf" in finding["description"]
    assert case.case_number in finding["description"]

    # Both documents' hashes are on file for future comparisons.
    assert db_session.query(DocumentPageHash).filter_by(document_id=first_document.id).count() == 1
    assert db_session.query(DocumentPageHash).filter_by(document_id=second_document.id).count() == 1


def test_match_reports_the_matched_documents_own_case_not_the_new_uploads_case(db_session, seeded_case):
    """Duplicate matches are deliberately cross-case (the whole point is
    catching the same document resubmitted into a DIFFERENT case) — the
    finding's matched_case_number must be the prior document's own case,
    not whatever case the new upload happens to be in."""
    first_case, user = seeded_case
    first_document = _make_document(db_session, first_case, user, filename="invoice_original.pdf")
    page = _textured_page(0)
    run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=first_document.id, pages=[page])

    second_case = Case(
        case_number="CASE-DUPCHECK-2",
        case_type=CaseType.vendor_invoice,
        submitted_by_user_id=user.id,
        status=CaseStatus.submitted,
    )
    db_session.add(second_case)
    db_session.flush()
    second_document = _make_document(db_session, second_case, user, filename="invoice_original_copy.pdf")

    result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=second_document.id, pages=[page])

    assert result["result"] == "flag"
    data = result["details"][0]["data"]
    assert data["matched_document_filename"] == "invoice_original.pdf"
    assert data["matched_case_id"] == str(first_case.id)
    assert data["matched_case_number"] == "CASE-DUPCHECK"


def test_genuinely_different_documents_do_not_match(db_session, seeded_case):
    case, user = seeded_case
    first_document = _make_document(db_session, case, user, filename="invoice_a.pdf")
    run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=first_document.id, pages=[_textured_page(1)])

    second_document = _make_document(db_session, case, user, filename="invoice_b.pdf")
    result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=second_document.id, pages=[_textured_page(2)])

    assert result == {"result": "pass", "details": []}


def test_real_sample_documents_do_not_false_positive(db_session, seeded_case, sample_document_pages):
    """Every real document in sample-documents/ is a distinct legitimate
    submission — none should read as a near-duplicate of another, even
    across the English/Arabic and Evidence/Invoice pairs that share a
    template. Regression guard for
    settings.duplicate_hash_hamming_threshold's default: confirmed during
    development that the closest distance between any two distinct real
    sample pages is 6 (Sample10 vs Sample6, both "Case_Evidence" —
    same template, different case data), so the default threshold of 5
    clears every one of them without false-flagging."""
    case, user = seeded_case
    for name, pages in sample_document_pages.items():
        document = _make_document(db_session, case, user, filename=name)
        result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=document.id, pages=pages)
        assert result["result"] == "pass", f"{name} unexpectedly flagged: {result['details']}"


def test_exact_duplicate_reupload_flags_with_distance_zero(db_session, seeded_case):
    case, user = seeded_case
    page = _textured_page(0)
    first_document = _make_document(db_session, case, user, filename="a.pdf")
    run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=first_document.id, pages=[page])

    second_document = _make_document(db_session, case, user, filename="a_copy.pdf")
    result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=second_document.id, pages=[page])

    assert result["result"] == "flag"
    assert result["details"][0]["data"]["distance"] == 0


def test_multi_page_document_matches_on_the_right_page(db_session, seeded_case):
    case, user = seeded_case
    first_document = _make_document(db_session, case, user, filename="multi_a.pdf")
    page_1, page_2 = _textured_page(10, page_number=1), _textured_page(20, page_number=2)
    run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=first_document.id, pages=[page_1, page_2])

    second_document = _make_document(db_session, case, user, filename="multi_b.pdf")
    # Only page 2 is a near-duplicate of the first document's page 2;
    # page 1 is unrelated synthetic content.
    new_page_1 = _textured_page(30, page_number=1)
    matching_page_2 = _near_duplicate(page_2, vendor_text="Meridian LLC", page_number=2)
    result = run_duplicate_check(db_session, company_id=db_session.info["test_company_id"], document_id=second_document.id, pages=[new_page_1, matching_page_2])

    assert result["result"] == "flag"
    assert len(result["details"]) == 1
    finding = result["details"][0]
    assert finding["page"] == 2
    assert finding["data"]["matched_document_id"] == str(first_document.id)
    assert finding["data"]["matched_page"] == 2


def test_finding_says_whether_the_same_file_was_reuploaded_and_by_whom(db_session, seeded_case):
    case, user = seeded_case
    user.full_name = "Test Reviewer"
    page = _textured_page(7)
    first = _make_document(db_session, case, user, filename="original.pdf", file_hash="a" * 64)
    run_duplicate_check(db_session, company_id=first.company_id, document_id=first.id, pages=[page])

    same = _make_document(db_session, case, user, filename="again.pdf", file_hash="a" * 64)
    [finding] = run_duplicate_check(db_session, company_id=same.company_id, document_id=same.id, pages=[page])["details"]
    assert finding["data"]["identical_file"] is True
    assert finding["data"]["matched_submitter"] == "Test Reviewer"
    assert "byte-for-byte identical (same SHA-256)" in finding["description"]
    assert "the exact same file re-uploaded" in finding["description"]
    assert "submitted by Test Reviewer" in finding["description"]

    similar = _make_document(db_session, case, user, filename="edited.pdf", file_hash="b" * 64)
    result = run_duplicate_check(
        db_session, company_id=similar.company_id, document_id=similar.id,
        pages=[_near_duplicate(page, vendor_text="ACME")],
    )
    finding = result["details"][0]
    assert finding["severity"] == "high"  # scoring unchanged
    assert finding["data"]["identical_file"] is False
    assert "different SHA-256" in finding["description"] and "perceptually similar" in finding["description"]
