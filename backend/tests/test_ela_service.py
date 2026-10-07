"""
Unit tests for app/services/forensics/ela.py — pure logic over
already-rendered page images, no DB/Celery/PDF rendering involved.

See ela.py's module docstring for the honest state of this check: a
first threshold approach (Otsu) false-positived on every real sample
document's ordinary text edges; the current page-relative-percentile
approach fixed that (confirmed below against real documents) and can
catch an obvious high-frequency splice (confirmed below), but did NOT
reliably catch a more realistic text-recompression splice in testing —
this suite does not assert sensitivity to that specific case, since it
isn't reliably true; asserting it would just be a flaky/misleading test.
"""
from __future__ import annotations

import cv2
import numpy as np

from app.services.forensics.ela import _detect_anti_forensics, run_ela_check
from app.services.forensics.pdf_render import RenderedPage

_WIDTH, _HEIGHT = 900, 1200
_JPEG_QUALITY = 90


def _resave(image: np.ndarray, quality: int = _JPEG_QUALITY) -> np.ndarray:
    _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def _textured_page(rng_seed: int = 0) -> np.ndarray:
    """Synthetic rendered-text content — see test_copy_move_service.py's
    identical fixture for why a flat/blocky synthetic image is a bad
    stand-in for real scanned/photographed content here (it reads as
    "smoothed" to detect_anti_forensics the way no real content would)."""
    rng = np.random.default_rng(rng_seed)
    image = np.full((_HEIGHT, _WIDTH, 3), 245, dtype=np.uint8)
    words = ["Invoice", "Total", "Amount", "Due", "Reference", "Vendor", "Tax", "Subtotal", "Payment", "Date"]
    for _ in range(150):
        x, y = int(rng.integers(20, _WIDTH - 160)), int(rng.integers(20, _HEIGHT - 20))
        text = f"{words[rng.integers(0, len(words))]} {rng.integers(1000, 9999)}"
        cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
    for _ in range(12):
        y = int(rng.integers(50, _HEIGHT - 50))
        cv2.line(image, (40, y), (_WIDTH - 40, y), (180, 180, 180), 1)
    return _resave(image)


def _page(image: np.ndarray, *, has_image_content: bool = True, page_number: int = 1) -> RenderedPage:
    h, w = image.shape[:2]
    return RenderedPage(page_number=page_number, image=image, width=w, height=h, has_image_content=has_image_content)


def test_not_applicable_when_no_page_has_embedded_image():
    """SPECIFICATION.md's exemption for born-digital, text-native pages — no
    embedded raster image anywhere means nothing for a recompression-
    error signal to exist in. Also this repo's actual behavior today:
    every one of the 12 real sample-documents/ PDFs takes this path."""
    page = _page(_textured_page(), has_image_content=False)
    result = run_ela_check([page])
    assert result["result"] == "not_applicable"
    [scope] = result["details"]
    assert scope["finding"] == "ela_scope" and scope["severity"] == "info"
    assert scope["data"]["reason"] == "born-digital, no images"


def test_clean_textured_page_passes():
    result = run_ela_check([_page(_textured_page())])
    assert result["result"] == "pass"
    assert result["details"] == []


def test_real_sample_documents_are_not_applicable_or_pass(sample_document_pages):
    """None of the real documents in sample-documents/ are tampered —
    every one must come back not_applicable (no embedded image) or pass,
    never flag. Regression test for the Otsu-threshold behavior found
    during development, which flagged 35 regions on one untampered
    real invoice page."""
    for name, pages in sample_document_pages.items():
        result = run_ela_check(pages)
        assert result["result"] in ("not_applicable", "pass"), (
            f"{name} unexpectedly flagged: {result['details']}"
        )


def test_high_frequency_splice_is_flagged_with_matching_bounding_box():
    """The one sensitivity case confirmed during development: fresh,
    high-frequency content (simulating a pasted-in photo/stamp/signature
    element) spliced into an already-JPEG-settled page. Bounding box
    should land on the actual spliced region, not just anywhere."""
    # A mostly-blank base with a little content (not the dense
    # _textured_page fixture) — representative of the realistic case
    # this check is actually meant for: a scanned form with a signature/
    # stamp in an otherwise blank area (SPECIFICATION.md section 3.1). Tried
    # against the busy _textured_page base during development: the
    # splice signal didn't clear the page's own noise floor there
    # either, for the same reason a realistic text splice didn't (see
    # module docstring) — this is the case that reliably works.
    rng = np.random.default_rng(1)
    base = np.full((_HEIGHT, _WIDTH, 3), 250, dtype=np.uint8)
    cv2.putText(base, "Approved", (60, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (20, 20, 20), 2, cv2.LINE_AA)
    base = _resave(base)
    tampered = base.copy()
    patch_y0, patch_y1, patch_x0, patch_x1 = 300, 560, 250, 550
    tampered[patch_y0:patch_y1, patch_x0:patch_x1] = rng.integers(
        0, 255, (patch_y1 - patch_y0, patch_x1 - patch_x0, 3), dtype=np.uint8
    )
    resaved = _resave(tampered)

    result = run_ela_check([_page(resaved)])

    assert result["result"] == "flag"
    region_findings = [f for f in result["details"] if f["finding"] == "recompression_error_region"]
    assert len(region_findings) >= 1
    bbox = region_findings[0]["bounding_box"]
    expected_x = ((patch_x0 + patch_x1) / 2) / _WIDTH
    expected_y = ((patch_y0 + patch_y1) / 2) / _HEIGHT
    box_center_x = bbox["x"] + bbox["width"] / 2
    box_center_y = bbox["y"] + bbox["height"] / 2
    # Generous tolerance (this is a probabilistic signal, not pixel-exact
    # segmentation) — the point is "roughly the right place on the page".
    assert abs(box_center_x - expected_x) < 0.15
    assert abs(box_center_y - expected_y) < 0.15


def test_anti_forensics_flags_smoothed_image():
    """Supporting signal folded into this check, not its own check_type
    (per spec) — reused as-is from the reference copy-move tool's
    detect_anti_forensics."""
    gray = cv2.cvtColor(_textured_page(), cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (9, 9), 3)
    warnings = _detect_anti_forensics(blurred)
    assert any("smoothing" in w for w in warnings)


def test_anti_forensics_quiet_on_untouched_texture():
    gray = cv2.cvtColor(_textured_page(), cv2.COLOR_BGR2GRAY)
    warnings = _detect_anti_forensics(gray)
    assert warnings == []
