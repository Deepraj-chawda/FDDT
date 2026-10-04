"""Pixel-identical signature reuse (classical CV, no model) and the
same-kind comparison plumbing around it."""
import uuid
from unittest.mock import MagicMock

import cv2
import numpy as np
import pymupdf
import pytest

from tests.conftest import tenant_task_factory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.signature_comparison_task as task_module
from app.models import Base
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.signature_match import SignatureMatch, SignatureMatchResult
from app.models.signature_reference import SignatureReference
from app.models.user import User, UserRole
from app.services.signature_comparison_service import (
    find_signature_reuse,
    printed_label_below,
    printed_labels_differ,
    signature_pixel_similarity,
)
from app.tasks.signature_comparison_task import (
    _get_detected_signature_bbox,
    run_signature_comparison,
)


def _squiggle(seed: int = 1, size=(120, 360)) -> np.ndarray:
    """A synthetic handwriting-like stroke on white; different seeds differ."""
    rng = np.random.default_rng(seed)
    img = np.full((*size, 3), 255, dtype=np.uint8)
    xs = np.linspace(10, size[1] - 10, 40)
    ys = size[0] / 2 + np.cumsum(rng.normal(0, 9, 40))
    ys = np.clip(ys, 10, size[0] - 10)
    pts = np.stack([xs, ys], axis=1).astype(np.int32)
    cv2.polylines(img, [pts], False, (150, 40, 20), 3, cv2.LINE_AA)
    return img


def _png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


# --------------------------------------------------------------------------
# signature_pixel_similarity
# --------------------------------------------------------------------------

def test_identical_image_scores_near_one_even_when_rescaled_or_recompressed():
    sig = _squiggle(1)
    assert signature_pixel_similarity(_png(sig), _png(sig)) > 0.99
    small = cv2.resize(sig, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA)
    assert signature_pixel_similarity(_png(sig), _png(small)) > 0.93
    jpeg = cv2.imdecode(cv2.imencode(".jpg", sig, [cv2.IMWRITE_JPEG_QUALITY, 60])[1], 1)
    assert signature_pixel_similarity(_png(sig), _png(jpeg)) > 0.93


def test_padding_and_small_shift_do_not_matter():
    sig = _squiggle(1)
    padded = cv2.copyMakeBorder(sig, 30, 10, 40, 5, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    assert signature_pixel_similarity(_png(sig), _png(padded)) > 0.99


def test_different_signatures_score_far_below_threshold():
    score = signature_pixel_similarity(_png(_squiggle(1)), _png(_squiggle(2)))
    assert score is not None and score < 0.8


def test_blank_or_undecodable_input_returns_none():
    blank = np.full((50, 100, 3), 255, dtype=np.uint8)
    assert signature_pixel_similarity(_png(_squiggle(1)), _png(blank)) is None
    assert signature_pixel_similarity(_png(_squiggle(1)), b"not an image") is None


# --------------------------------------------------------------------------
# printed signer text
# --------------------------------------------------------------------------

def _signed_pdf(name_line: str, sig_png: bytes) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(pymupdf.Rect(60, 400, 240, 460), stream=sig_png)  # y 0.50-0.575
    page.insert_text((62, 476), name_line, fontsize=10)  # just under the signature
    page.insert_text((62, 380), "Authorised signatory", fontsize=10)  # heading ABOVE it
    return doc.tobytes()


SIG_BOX = {"page": 1, "x": 0.10, "y": 0.50, "width": 0.30, "height": 0.075}


def test_printed_label_reads_line_below_not_heading_above():
    pdf = _signed_pdf("Sultan Al-Dhaheri", _png(_squiggle(1)))
    assert printed_label_below(pdf, SIG_BOX) == "Sultan Al-Dhaheri"


def test_printed_labels_differ_only_when_both_present_and_dissimilar():
    assert printed_labels_differ("Sultan Al-Dhaheri", "Saeed Al-Mansoori")
    assert not printed_labels_differ("Sultan Al-Dhaheri", "Sultan Al-Dhaheri")
    assert not printed_labels_differ("", "Saeed Al-Mansoori")  # unreadable proves nothing


# --------------------------------------------------------------------------
# find_signature_reuse
# --------------------------------------------------------------------------

class FakeStorage:
    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs

    def download_bytes(self, url: str) -> bytes:
        return self.blobs[url]


def _reuse(storage, ref_png, ref_pdf, target_png_url="t.png", target_pdf_url="t.pdf"):
    return find_signature_reuse(
        storage,
        reference_image_bytes=ref_png,
        reference_pdf_bytes=ref_pdf,
        reference_box=SIG_BOX,
        target_image_url=target_png_url,
        target_pdf_url=target_pdf_url,
        target_box=SIG_BOX,
    )


def test_identical_signature_under_different_names_is_reused_different_signer():
    png = _png(_squiggle(1))
    storage = FakeStorage({
        "t.png": png,
        "t.pdf": _signed_pdf("Saeed Al-Mansoori", png),
    })
    verdict, reasoning = _reuse(storage, png, _signed_pdf("Sultan Al-Dhaheri", png))
    assert verdict == "reused_different_signer"
    assert "Sultan Al-Dhaheri" in reasoning and "Saeed Al-Mansoori" in reasoning
    assert "forged" not in reasoning.lower() and "verified" not in reasoning.lower()


def test_identical_signature_under_same_name_is_plain_identical_reuse():
    png = _png(_squiggle(1))
    storage = FakeStorage({"t.png": png, "t.pdf": _signed_pdf("Sultan Al-Dhaheri", png)})
    verdict, _ = _reuse(storage, png, _signed_pdf("Sultan Al-Dhaheri", png))
    assert verdict == "identical_reuse"


def test_unreadable_names_fall_back_to_identical_reuse():
    png = _png(_squiggle(1))
    storage = FakeStorage({"t.png": png, "t.pdf": b"%PDF-1.4 not really"})
    verdict, reasoning = _reuse(storage, png, None)
    assert verdict == "identical_reuse"
    assert "could not be read" in reasoning


def test_different_signature_is_not_reuse():
    storage = FakeStorage({"t.png": _png(_squiggle(2)), "t.pdf": _signed_pdf("X Y", _png(_squiggle(2)))})
    assert _reuse(storage, _png(_squiggle(1)), None) is None


def test_storage_failure_returns_none_instead_of_raising():
    storage = FakeStorage({})  # KeyError on download
    assert _reuse(storage, _png(_squiggle(1)), None) is None


# --------------------------------------------------------------------------
# the task: same-kind box + reuse verdict end to end
# --------------------------------------------------------------------------

@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    monkeypatch.setattr(task_module, "SessionLocal", factory)
    return factory


STAMP_BOX = {"page": 1, "x": 0.60, "y": 0.50, "width": 0.20, "height": 0.20}


def _detection(document_id, detected):
    return DocumentCheck(
        document_id=document_id,
        check_type=DocumentCheckType.signature_stamp_detection,
        status=DocumentCheckStatus.completed,
        result={"result": "pass", "details": {"detected": detected, "bounding_box": detected[0]["bounding_box"]}},
    )


def _build_case(session, ref_kind_box, target_regions):
    user = User(email=f"{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", role=UserRole.reviewer_l2, is_active=True)
    session.add(user)
    session.flush()
    case = Case(
        id=uuid.uuid4(), case_number=f"CASE-{uuid.uuid4().hex[:8].upper()}",
        case_type=CaseType.vendor_invoice, submitted_by_user_id=user.id, status=CaseStatus.submitted,
    )
    session.add(case)

    def doc(name):
        d = Document(
            id=uuid.uuid4(), case_id=case.id, uploaded_by_user_id=user.id, original_filename=name,
            content_type="application/pdf", file_size_bytes=1, file_hash=name, blob_storage_path=name,
        )
        session.add(d)
        return d

    ref_doc, target_doc = doc("ref.pdf"), doc("target.pdf")
    session.flush()
    session.add(_detection(ref_doc.id, [
        {"kind": "signature", "confidence": "high", "bounding_box": SIG_BOX},
        {"kind": "stamp", "confidence": "high", "bounding_box": STAMP_BOX},
    ]))
    session.add(_detection(target_doc.id, target_regions))
    ref = SignatureReference(
        id=uuid.uuid4(), person_name="Reviewer typed", source_case_id=case.id, source_document_id=ref_doc.id,
        created_by=user.id, signature_image_url="ref.png", bounding_box=ref_kind_box, is_library=False,
    )
    session.add(ref)
    session.commit()
    return case.id, ref.id, target_doc.id


def _patch_task(monkeypatch, blobs, crop_calls):
    monkeypatch.setattr(task_module, "get_storage_service_for_task", lambda: FakeStorage(blobs))

    def fake_crop(storage, blob_path, bbox, reference_id, company_id=None, case_id=None):
        crop_calls.append(bbox)
        return "target.png"

    monkeypatch.setattr(task_module, "crop_and_upload_signature", fake_crop)
    monkeypatch.setattr(task_module, "request_case_scoring", lambda case_id, company_id: None)
    llm = MagicMock()
    monkeypatch.setattr(task_module, "get_llm_service", lambda: llm)
    return llm


def test_task_flags_reused_signature_under_different_signer_without_calling_the_model(
    monkeypatch, session_factory
):
    png = _png(_squiggle(1))
    other_sig_box = {"page": 1, "x": 0.11, "y": 0.51, "width": 0.30, "height": 0.075}
    session = session_factory()
    case_id, ref_id, target_id = _build_case(
        session,
        SIG_BOX,
        # The target's highest-confidence region is a STAMP; the signature is lower.
        [
            {"kind": "stamp", "confidence": "high", "bounding_box": STAMP_BOX},
            {"kind": "signature", "confidence": "medium", "bounding_box": other_sig_box},
        ],
    )
    session.close()

    crop_calls: list[dict] = []
    llm = _patch_task(monkeypatch, {
        "ref.png": png, "target.png": png,
        "ref.pdf": _signed_pdf("Sultan Al-Dhaheri", png),
        "target.pdf": _signed_pdf("Saeed Al-Mansoori", png),
    }, crop_calls)

    run_signature_comparison(str(ref_id))

    assert crop_calls == [other_sig_box]  # the signature region, not the higher-confidence stamp
    llm.compare_signatures.assert_not_called()
    check = session_factory()
    try:
        [match] = check.query(SignatureMatch).filter(SignatureMatch.case_id == case_id).all()
        assert match.document_id == target_id
        assert match.result == SignatureMatchResult.reused_different_signer
        assert "Sultan Al-Dhaheri" in match.reasoning and "Saeed Al-Mansoori" in match.reasoning
    finally:
        check.close()


def test_task_never_flags_reuse_for_a_stamp_reference(monkeypatch, session_factory):
    """Company stamps are legitimately identical every time — a stamp reference
    goes to the model, not the pixel-reuse check."""
    png = _png(_squiggle(1))
    session = session_factory()
    _case_id, ref_id, _target = _build_case(
        session,
        STAMP_BOX,  # reference drawn around the stamp
        [{"kind": "stamp", "confidence": "high", "bounding_box": STAMP_BOX}],
    )
    session.close()
    crop_calls: list[dict] = []
    llm = _patch_task(monkeypatch, {"ref.png": png, "target.png": png, "ref.pdf": b"", "target.pdf": b""}, crop_calls)
    from app.services.llm_service import SignatureComparisonResult

    llm.compare_signatures.return_value = SignatureComparisonResult(result="consistent", reasoning="same stamp design")

    run_signature_comparison(str(ref_id))

    assert crop_calls == [STAMP_BOX]
    llm.compare_signatures.assert_called_once()
    check = session_factory()
    try:
        assert check.query(SignatureMatch).one().result == SignatureMatchResult.consistent
    finally:
        check.close()


def test_signature_reference_skips_documents_with_only_a_stamp(session_factory):
    session = session_factory()
    try:
        _case_id, _ref_id, target_id = _build_case(
            session, SIG_BOX, [{"kind": "stamp", "confidence": "high", "bounding_box": STAMP_BOX}]
        )
        assert _get_detected_signature_bbox(session, target_id, "signature") is None
        assert _get_detected_signature_bbox(session, target_id, "stamp") == STAMP_BOX
        assert _get_detected_signature_bbox(session, target_id) == STAMP_BOX  # no kind -> primary
    finally:
        session.close()


def test_new_verdicts_are_scoreable_by_seed_rules():
    from app.services.risk_rule_seed import SEED_RULES

    by_id = {r["rule_id"]: r for r in SEED_RULES}
    for verdict, rule_id in (
        ("identical_reuse", "signature.identical_reuse"),
        ("reused_different_signer", "signature.reused_different_signer"),
    ):
        assert by_id[rule_id]["condition"] == {"match": "signature_match", "result": verdict}
    assert by_id["signature.reused_different_signer"]["weight"] > by_id["signature.identical_reuse"]["weight"]
