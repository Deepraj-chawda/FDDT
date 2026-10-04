import uuid
from unittest.mock import MagicMock

import numpy as np

from app.services.forensics.pdf_render import RenderedPage
from app.services.llm_service import (
    DetectedSignatureRegion,
    NormalizedBoundingBox,
    PageSignatureDetection,
)
from app.services.signature_detection_service import detect_signatures
from tests.sample_files import PDF


def _page(number: int = 1) -> RenderedPage:
    return RenderedPage(
        page_number=number,
        image=np.full((100, 80, 3), 255, dtype=np.uint8),
        width=80,
        height=100,
        has_image_content=False,
    )


def _region(kind="signature", confidence="high", x=0.1, y=0.2, w=0.3, h=0.1):
    return DetectedSignatureRegion(
        kind=kind,
        description="ink mark",
        confidence=confidence,
        bounding_box=NormalizedBoundingBox(x=x, y=y, width=w, height=h),
    )


def _llm(*detections: PageSignatureDetection) -> MagicMock:
    llm = MagicMock()
    llm.detect_signatures_stamps.side_effect = list(detections)
    return llm


def test_presence_is_a_pass_and_exposes_primary_bbox():
    llm = _llm(PageSignatureDetection(signature_expected=True, regions=[_region()]))
    out = detect_signatures(llm, [_page(1)])
    assert out["result"] == "pass"
    box = out["details"]["bounding_box"]
    assert box["page"] == 1
    # Padded slightly around the model's box, never smaller than it.
    assert box["x"] <= 0.1 and box["y"] <= 0.2
    assert box["x"] + box["width"] >= 0.4 and box["y"] + box["height"] >= 0.3
    assert box["width"] < 0.45 and box["height"] < 0.2  # modest padding only
    assert out["details"]["detected"][0]["kind"] == "signature"


def test_expected_but_missing_flags():
    llm = _llm(PageSignatureDetection(signature_expected=True, regions=[]))
    out = detect_signatures(llm, [_page()])
    assert out["result"] == "flag"
    assert out["details"]["bounding_box"] is None


def test_not_expected_and_missing_passes():
    llm = _llm(PageSignatureDetection(signature_expected=False, regions=[]))
    out = detect_signatures(llm, [_page()])
    assert out["result"] == "pass"
    assert out["details"]["bounding_box"] is None


def test_signature_on_later_page_satisfies_expectation_from_first_page():
    llm = _llm(
        PageSignatureDetection(signature_expected=True, regions=[]),
        PageSignatureDetection(signature_expected=False, regions=[_region()]),
    )
    out = detect_signatures(llm, [_page(1), _page(2)])
    assert out["result"] == "pass"
    assert out["details"]["bounding_box"]["page"] == 2


def test_primary_prefers_higher_confidence_then_signature_over_stamp():
    llm = _llm(
        PageSignatureDetection(
            signature_expected=True,
            regions=[
                _region(kind="stamp", confidence="high", x=0.5),
                _region(kind="signature", confidence="high", x=0.1),
                _region(kind="signature", confidence="low", x=0.7),
            ],
        )
    )
    out = detect_signatures(llm, [_page()])
    assert abs(out["details"]["bounding_box"]["x"] - 0.1) < 0.03  # the high-confidence signature, not the stamp/low one


def test_box_is_clamped_and_degenerate_boxes_dropped():
    llm = _llm(
        PageSignatureDetection(
            signature_expected=True,
            regions=[_region(x=0.9, w=0.5), _region(x=0.2, w=0.0001)],
        )
    )
    out = detect_signatures(llm, [_page()])
    assert len(out["details"]["detected"]) == 1
    box = out["details"]["bounding_box"]
    assert box["x"] + box["width"] <= 1.0


def _pdf_with_signature_image() -> tuple[bytes, RenderedPage, dict]:
    """A real one-page PDF with a dark 'signature' PNG placed at a known
    rectangle, plus its render and that rectangle in normalized coords."""
    import cv2
    import pymupdf

    from app.services.forensics.pdf_render import render_pdf_pages

    png = cv2.imencode(".png", np.full((40, 120, 3), 30, dtype=np.uint8))[1].tobytes()
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    rect = pymupdf.Rect(60, 400, 240, 460)  # x 0.10-0.40, y 0.50-0.575
    page.insert_image(rect, stream=png)
    pdf_bytes = doc.tobytes()
    truth = {"x": 0.10, "y": 0.50, "width": 0.30, "height": 0.075}
    return pdf_bytes, render_pdf_pages(pdf_bytes)[0], truth


def test_embedded_image_replaces_a_misplaced_model_box():
    pdf_bytes, page, truth = _pdf_with_signature_image()
    # The model points ~0.06 too low — the real failure (box over the printed name).
    llm = _llm(
        PageSignatureDetection(
            signature_expected=True,
            regions=[_region(x=0.08, y=0.56, w=0.24, h=0.13)],
        )
    )
    out = detect_signatures(llm, [page], pdf_bytes)
    entry = out["details"]["detected"][0]
    box = entry["bounding_box"]
    assert entry["refinement"] == "embedded_image"
    assert llm.detect_signatures_stamps.call_count == 1  # no second look needed
    assert abs(box["x"] - truth["x"]) < 0.01 and abs(box["y"] - truth["y"]) < 0.01
    assert abs(box["width"] - truth["width"]) < 0.02 and abs(box["height"] - truth["height"]) < 0.02


def test_signature_and_stamp_do_not_snap_to_the_same_image():
    pdf_bytes, page, _truth = _pdf_with_signature_image()
    llm = _llm(
        PageSignatureDetection(
            signature_expected=True,
            regions=[
                _region(kind="signature", x=0.10, y=0.50, w=0.30, h=0.075),
                _region(kind="stamp", x=0.12, y=0.50, w=0.25, h=0.075),
            ],
        ),
        # The stamp finds no image of its own, so it gets a second look.
        PageSignatureDetection(signature_expected=True, regions=[]),
    )
    out = detect_signatures(llm, [page], pdf_bytes)
    by_kind = {d["kind"]: d["refinement"] for d in out["details"]["detected"]}
    assert by_kind["signature"] == "embedded_image"
    assert by_kind["stamp"] == "none"


def test_second_look_relocates_box_and_maps_back_to_page_coordinates():
    # No PDF bytes -> no embedded images, so the zoomed second look runs.
    image = np.full((1000, 800, 3), 255, dtype=np.uint8)
    image[500:560, 100:300] = 30  # ink at x 0.125-0.375, y 0.50-0.56
    page = RenderedPage(page_number=1, image=image, width=800, height=1000, has_image_content=False)
    first_pass = _region(x=0.10, y=0.56, w=0.25, h=0.10)  # overlaps only the ink's last sliver
    # In the zoomed window (x from 0.0, y from 0.48, 0.45 x 0.26 of the page) the
    # ink sits at fractions x 0.278-0.833, y 0.077-0.308.
    llm = _llm(
        PageSignatureDetection(signature_expected=True, regions=[first_pass]),
        PageSignatureDetection(
            signature_expected=True,
            regions=[_region(x=0.278, y=0.077, w=0.555, h=0.231)],
        ),
    )
    out = detect_signatures(llm, [page])
    entry = out["details"]["detected"][0]
    box = entry["bounding_box"]
    assert entry["refinement"] == "second_look"
    assert llm.detect_signatures_stamps.call_count == 2
    # Trimmed to the ink: x 0.125-0.375, y 0.50-0.56 (plus a hair of margin).
    assert abs(box["x"] - 0.125) < 0.01 and abs(box["y"] - 0.50) < 0.01
    assert abs(box["x"] + box["width"] - 0.375) < 0.01
    assert abs(box["y"] + box["height"] - 0.56) < 0.01


def test_blank_crop_skips_second_look_and_keeps_padded_first_box():
    llm = _llm(PageSignatureDetection(signature_expected=True, regions=[_region()]))
    out = detect_signatures(llm, [_page()])
    assert out["details"]["detected"][0]["refinement"] == "none"
    assert llm.detect_signatures_stamps.call_count == 1


def test_second_look_failure_falls_back_to_first_box():
    image = np.full((1000, 800, 3), 255, dtype=np.uint8)
    image[500:560, 100:300] = 30
    page = RenderedPage(page_number=1, image=image, width=800, height=1000, has_image_content=False)
    llm = MagicMock()
    llm.detect_signatures_stamps.side_effect = [
        PageSignatureDetection(signature_expected=True, regions=[_region(x=0.10, y=0.50, w=0.30, h=0.08)]),
        RuntimeError("vision model unavailable"),
    ]
    out = detect_signatures(llm, [page])
    entry = out["details"]["detected"][0]
    assert entry["refinement"] == "none"
    assert entry["bounding_box"]["x"] <= 0.10  # padded first-pass box retained


def test_file_url_endpoint_returns_signed_url_and_404s_for_wrong_case(client, auth_headers):
    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers).json()
    other = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers).json()
    doc = client.post(
        f"/cases/{case['id']}/documents",
        headers=auth_headers,
        files={"file": ("invoice.pdf", PDF, "application/pdf")},
    ).json()

    ok = client.get(f"/cases/{case['id']}/documents/{doc['id']}/file-url", headers=auth_headers)
    assert ok.status_code == 200
    assert "?fake-sas-token" in ok.json()["file_url"]

    wrong_case = client.get(f"/cases/{other['id']}/documents/{doc['id']}/file-url", headers=auth_headers)
    assert wrong_case.status_code == 404
    missing = client.get(f"/cases/{case['id']}/documents/{uuid.uuid4()}/file-url", headers=auth_headers)
    assert missing.status_code == 404
