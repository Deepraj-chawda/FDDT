"""Post-filters on the vision-model review (apply_region_filters in
app/services/visual_inconsistency_service.py): alignment/sharpness findings
about a signature or stamp, and tilt findings that are only the scan's skew."""
import copy
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.forensics.pdf_render import RenderedPage, render_pdf_pages
from app.services.visual_inconsistency_service import apply_region_filters, estimate_skew

SAMPLES = Path(__file__).resolve().parents[2] / "sample-documents" / "tampered_test_samples"


def _finding(category, box, description, severity="high"):
    return {
        "finding": f"visual_{category}",
        "severity": severity,
        "description": description,
        "page": 1,
        "bounding_box": {"page": 1, **box},
        "data": {"category": category},
    }


def _result(*findings, correlation=True):
    details = [{"finding": "experimental_signal_notice", "severity": "info", "description": "note"}, *findings]
    if correlation:
        details.append({"finding": "metadata_forensics_correlation", "severity": "high", "description": "both flagged"})
    return {"result": "flag", "details": details}


def _signatures(*boxes):
    return {"result": "pass", "details": {"detected": [
        {"kind": kind, "bounding_box": {"page": 1, **box}} for kind, box in boxes
    ]}}


def _lines_page(angle_deg=0.0, block=None, size=(1400, 1000)):
    """A white page of evenly spaced 'text lines' (dark bars), rotated by
    `angle_deg`; `block` = (y0, y1, angle) re-draws that band of lines at its
    own extra angle."""
    h, w = size
    img = np.full((h, w), 255, np.uint8)
    for y in range(120, h - 120, 40):
        for x in range(100, w - 100, 90):
            cv2.rectangle(img, (x, y), (x + 70, y + 12), 0, -1)
    if block is not None:
        y0, y1, extra = block
        band = img[int(y0 * h):int(y1 * h)].copy()
        m = cv2.getRotationMatrix2D((w / 2, band.shape[0] / 2), extra, 1.0)
        img[int(y0 * h):int(y1 * h)] = cv2.warpAffine(band, m, (w, band.shape[0]), borderValue=255)
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    img = cv2.warpAffine(img, m, (w, h), borderValue=255)
    return [RenderedPage(page_number=1, image=cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), width=w, height=h,
                         has_image_content=True)]


# --- signature / stamp overlap -------------------------------------------------------


def test_alignment_finding_about_signatures_is_not_counted():
    # CASE-DE627FA2: the model's box sits below the refined signature boxes.
    finding = _finding("text_alignment", {"x": 0.1, "y": 0.8, "width": 0.4, "height": 0.15},
                       "The signatures at the bottom are misaligned with the typed text.")
    sig = _signatures(("signature", {"x": 0.079, "y": 0.67, "width": 0.138, "height": 0.1}))
    result = apply_region_filters(_result(finding), sig, [])
    assert result["result"] == "pass"
    assert finding["severity"] == "info"
    assert "baseline" in finding["data"]["region_filter"]["reason"]
    assert finding["data"]["region_filter"]["original_severity"] == "high"
    # The correlation note goes with the flag it correlated.
    assert all(f["finding"] != "metadata_forensics_correlation" for f in result["details"])


def test_sharpness_finding_about_a_stamp_is_not_counted():
    finding = _finding("resolution_sharpness_consistency", {"x": 0.35, "y": 0.68, "width": 0.3, "height": 0.1},
                       "The signature and stamp near the bottom appear blurrier.")
    sig = _signatures(("stamp", {"x": 0.492, "y": 0.811, "width": 0.054, "height": 0.051}))
    assert apply_region_filters(_result(finding), sig, [])["result"] == "pass"
    assert "softer than printed text" in finding["data"]["region_filter"]["reason"]


@pytest.mark.parametrize("box, text", [
    # Far from the signature.
    ({"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.05}, "The total amount is misaligned with its row."),
    # A broad box that happens to include the signature but is about something else.
    ({"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}, "The amounts column is offset from the labels."),
])
def test_other_findings_are_kept(box, text):
    finding = _finding("text_alignment", box, text)
    sig = _signatures(("signature", {"x": 0.08, "y": 0.67, "width": 0.14, "height": 0.1}))
    result = apply_region_filters(_result(finding), sig, [])
    assert result["result"] == "flag" and finding["severity"] == "high"
    assert any(f["finding"] == "metadata_forensics_correlation" for f in result["details"])


def test_other_categories_are_never_filtered():
    finding = _finding("color_contrast_consistency", {"x": 0.08, "y": 0.67, "width": 0.14, "height": 0.1},
                       "The stamp area has a different background shade.")
    sig = _signatures(("stamp", {"x": 0.08, "y": 0.67, "width": 0.14, "height": 0.1}))
    assert apply_region_filters(_result(finding), sig, [])["result"] == "flag"


def test_filtering_is_reversible_when_rerun_without_regions():
    finding = _finding("text_alignment", {"x": 0.1, "y": 0.8, "width": 0.4, "height": 0.15},
                       "The signatures are misaligned.")
    original = copy.deepcopy(finding)
    sig = _signatures(("signature", {"x": 0.079, "y": 0.67, "width": 0.138, "height": 0.1}))
    result = apply_region_filters(_result(finding, correlation=False), sig, [])
    again = apply_region_filters(copy.deepcopy(result), sig, [])
    assert again == result  # idempotent
    restored = apply_region_filters(result, None, [])
    assert restored["result"] == "flag"
    assert restored["details"][1]["severity"] == original["severity"]
    assert restored["details"][1]["description"] == original["description"]


# --- scan skew --------------------------------------------------------------------------


@pytest.mark.parametrize("angle", [0.0, 1.2, -2.0])
def test_estimate_skew_reads_the_rotation(angle):
    page = _lines_page(angle)[0]
    gray = cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY)
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    # Image y grows downward, so a counter-clockwise rotation reads negative.
    assert estimate_skew(ink) == pytest.approx(-angle, abs=0.15)


def test_tilt_that_matches_the_whole_page_skew_is_not_counted():
    finding = _finding("text_alignment", {"x": 0.2, "y": 0.3, "width": 0.6, "height": 0.1},
                       "The text in this block is slightly tilted.")
    result = apply_region_filters(_result(finding), None, _lines_page(1.2))
    assert result["result"] == "pass"
    assert "skew from scanning" in finding["data"]["region_filter"]["reason"]


def test_a_block_tilted_against_its_surroundings_is_kept():
    # Lines in the band 0.45-0.6 rotated 2.5° on an otherwise straight page.
    finding = _finding("text_alignment", {"x": 0.2, "y": 0.47, "width": 0.6, "height": 0.1},
                       "This block is rotated relative to the rest of the page.")
    result = apply_region_filters(_result(finding), None, _lines_page(0.0, block=(0.45, 0.6, 2.5)))
    assert result["result"] == "flag" and finding["severity"] == "high"


@pytest.mark.parametrize("text", [
    "The amount is shifted to the right of its column.",
    # Sample16/17: a retyped row sits off the baseline of a straight digital page.
    "The text in the second item description cell is misaligned vertically compared to the first and third items.",
    "The line is slightly lower than the line above it, indicating misalignment in the itemized list.",
])
def test_offset_findings_are_never_judged_by_skew(text):
    finding = _finding("text_alignment", {"x": 0.2, "y": 0.3, "width": 0.6, "height": 0.1}, text)
    assert apply_region_filters(_result(finding), None, _lines_page(0.0))["result"] == "flag"


# --- the reference cases ------------------------------------------------------------------


def test_reference_cases_visual_findings_are_explained():
    headstart = render_pdf_pages((SAMPLES / "case1.pdf").read_bytes())
    tilt = _finding("text_alignment", {"x": 0.23, "y": 0.16, "width": 0.54, "height": 0.07},
                    "The text in the boxed section with student name and ID is slightly rotated.")
    soft = _finding("resolution_sharpness_consistency", {"x": 0.35, "y": 0.68, "width": 0.3, "height": 0.1},
                    "The signature and stamp near the bottom center appear blurrier.")
    sig = _signatures(
        ("stamp", {"x": 0.492, "y": 0.811, "width": 0.054, "height": 0.051}),
        ("signature", {"x": 0.366, "y": 0.831, "width": 0.15, "height": 0.072}),
    )
    assert apply_region_filters(_result(tilt, soft), sig, headstart)["result"] == "pass"
    assert "skew" in tilt["data"]["region_filter"]["reason"]
    assert "stamp" in soft["data"]["region_filter"]["reason"]


# --- the two tasks finishing in either order -------------------------------------------


def test_signature_detection_refilters_a_visual_review_that_finished_first(db_session, seeded_user):
    user = seeded_user
    from app.models.document_check import DocumentCheck, DocumentCheckType
    from app.services.check_store import save_check
    from app.models.document_check import DocumentCheckStatus
    from app.tasks.visual_inconsistency_task import refilter_visual_review
    from tests.helpers_risk import add_document, make_case

    finding = _finding("text_alignment", {"x": 0.1, "y": 0.8, "width": 0.4, "height": 0.15},
                       "The signatures at the bottom are misaligned with the typed text.")
    case = make_case(db_session, user)
    doc = add_document(
        db_session, case, user, "invoice.pdf",
        checks={DocumentCheckType.visual_inconsistency_review: _result(finding)},
    )

    def visual():
        return db_session.query(DocumentCheck).filter_by(
            document_id=doc.id, check_type=DocumentCheckType.visual_inconsistency_review
        ).one().result

    # No detection yet: nothing to change.
    assert refilter_visual_review(db_session, doc, []) is False
    save_check(
        db_session, document=doc, check_type=DocumentCheckType.signature_stamp_detection,
        status=DocumentCheckStatus.completed,
        result=_signatures(("signature", {"x": 0.079, "y": 0.67, "width": 0.138, "height": 0.1})),
    )
    assert refilter_visual_review(db_session, doc, []) is True
    db_session.commit()
    db_session.expire_all()
    stored = visual()
    assert stored["result"] == "pass"
    assert stored["details"][1]["severity"] == "info"
    # Running it again changes nothing.
    assert refilter_visual_review(db_session, doc, []) is False
