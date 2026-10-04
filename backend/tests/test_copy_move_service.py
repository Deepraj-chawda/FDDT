"""
Unit tests for app/services/forensics/copy_move.py — pure logic over
already-rendered page images, no DB/Celery/PDF rendering involved.
Fixtures are built in-memory with numpy/cv2 (a textured base plus a
pasted duplicate for the "flag" cases) so this suite is deterministic
and self-contained.

Two things this file exists specifically to guard against regressing
(both found and fixed during development — see copy_move.py's
BRISK_THRESHOLD/CLUSTER_SIZE/MIN_REGION_AREA_FRACTION comments):
1. A text-heavy real page can produce tens of thousands of BRISK
   keypoints, and unbounded matching/clustering over that many keypoints
   hung a worker outright in testing — test_real_documents_do_not_hang
   below is a real regression test for that, not just a speed check.
2. The reference tool's default CLUSTER_SIZE (5) flagged every one of
   this repo's 12 real sample documents as "copy-move" (2 to 219
   clusters each) — ordinary repeated small shapes (digits, table
   cells) read as "duplicated content" at a low match-count threshold.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from app.services.forensics.copy_move import run_copy_move_check
from app.services.forensics.pdf_render import RenderedPage

_WIDTH, _HEIGHT = 900, 1200


def _textured_page(rng_seed: int = 0) -> np.ndarray:
    """Synthetic rendered-text content — real crisp edges for BRISK to
    find keypoints on (validated against real rendered PDF pages during
    development), unlike a flat/blocky synthetic fill (too little
    texture for BRISK, and reads as "smoothed" to detect_anti_forensics
    the way no real scanned/photographed page would)."""
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
    return image


def _page(image: np.ndarray, *, page_number: int = 1) -> RenderedPage:
    h, w = image.shape[:2]
    return RenderedPage(page_number=page_number, image=image, width=w, height=h, has_image_content=True)


def test_clean_textured_page_passes():
    result = run_copy_move_check([_page(_textured_page())])
    assert result["result"] == "pass"
    assert result["details"] == []


def test_real_sample_documents_do_not_false_positive(sample_document_pages):
    """Every real document in sample-documents/ is a legitimate business
    document — none should read as copy-move forgery. Regression test
    for the CLUSTER_SIZE=5 false-positive-on-everything behavior found
    during development (see module docstring)."""
    for name, pages in sample_document_pages.items():
        result = run_copy_move_check(pages)
        assert result["result"] == "pass", f"{name} unexpectedly flagged: {result['details']}"


def test_real_documents_do_not_hang(sample_document_pages):
    """Regression test for the actual incident during development: an
    unbounded BRISK+radiusMatch+greedy-clustering pipeline hung a worker
    process on a real text-heavy invoice page (still running after 90s
    on a single page). Every real sample must finish well within a
    generous budget."""
    for name, pages in sample_document_pages.items():
        start = time.time()
        run_copy_move_check(pages)
        elapsed = time.time() - start
        assert elapsed < 10, f"{name} took {elapsed:.1f}s — copy-move detection must stay bounded"


def _paste_copy(image: np.ndarray) -> np.ndarray:
    """A large, content-rich patch copied elsewhere on the same page —
    a textbook copy-move forgery."""
    tampered = image.copy()
    patch = image[400:620, 200:550].copy()
    tampered[900:1120, 900:1250] = patch
    return tampered


def _first_real_page(sample_document_pages) -> RenderedPage:
    """A real rendered invoice page — used for the sensitivity ("flag")
    tests below instead of the synthetic _textured_page fixture. A
    synthetic fixture dense enough to reliably reproduce real documents'
    keypoint density (900-5,000 filtered keypoints — see
    BRISK_THRESHOLD's comment) without tripping MAX_FILTERED_KEYPOINTS
    turned out to need much more careful tuning than a real page already
    provides for free; several synthetic attempts during development
    were either too sparse (no cluster reached CLUSTER_SIZE) or too
    dense (hit the safety cap outright)."""
    name = next(n for n in sample_document_pages if n.endswith("Invoice.pdf"))
    return sample_document_pages[name][0]


def test_pasted_duplicate_region_is_flagged(sample_document_pages):
    page = _first_real_page(sample_document_pages)
    tampered = _paste_copy(page.image)

    result = run_copy_move_check([_page(tampered, page_number=page.page_number)])

    assert result["result"] == "flag"
    assert len(result["details"]) >= 1
    finding = result["details"][0]
    assert finding["finding"] == "copy_move_cluster"
    assert "bounding_box" in finding
    assert finding["bounding_box"]["page"] == page.page_number
    assert finding["data"]["matched_pairs"] >= 30  # >= CLUSTER_SIZE


def test_overlapping_clusters_are_deduplicated(sample_document_pages):
    """One real duplicated region reliably produces many overlapping
    greedy clusters before dedup (confirmed in development: 21 for one
    pasted patch) — this must collapse to a small number of genuinely
    distinct findings, not flood the reviewer with near-identical rows."""
    page = _first_real_page(sample_document_pages)
    tampered = _paste_copy(page.image)

    result = run_copy_move_check([_page(tampered, page_number=page.page_number)])

    assert 1 <= len(result["details"]) <= 3
