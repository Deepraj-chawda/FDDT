"""
Shared PDF-page-to-image rasterization for the image-tampering checks
(ELA + copy-move — app/services/forensics/ela.py, copy_move.py).
app/tasks/tampering_checks_task.py renders once via this module and
hands the same in-memory page list to both checks, instead of each
re-rendering the PDF itself — the render is the expensive part.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf

# 200 DPI balances enough pixel detail for ELA's recompression-error map
# and BRISK keypoint detection against memory/CPU cost across a
# multi-page document — in the same range as a real scanned page, so
# ELA's JPEG-block-artifact scale is representative of what a genuine
# scan/rescan would show (SPECIFICATION.md section 3.2 flags print-and-rescan
# as ELA's known weak spot; rendering far above real scan resolution
# would only make that worse).
RENDER_DPI = 200


@dataclass
class RenderedPage:
    """One page rendered once. `page_number` is 1-based — how a
    reviewer/PDF viewer counts pages, and how ela.py/copy_move.py's
    Finding descriptions and bounding_box.page refer to it — not the
    same as a 0-based list index. `has_image_content` gates ela.py's
    analysis (SPECIFICATION.md's "born-digital, text-native pages" exemption):
    true iff this PDF page has at least one embedded raster image
    XObject; copy_move.py ignores this flag and runs on every page (a
    duplicated paragraph/logo/signature shows up as a keypoint cluster
    whether the source content is a photo or rendered text/vector art)."""

    page_number: int
    image: np.ndarray  # BGR uint8, OpenCV's native layout
    width: int
    height: int
    has_image_content: bool


def render_pdf_pages(pdf_bytes: bytes) -> list[RenderedPage]:
    pages: list[RenderedPage] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        zoom = RENDER_DPI / 72  # a PDF's native unit is 1/72 inch
        matrix = pymupdf.Matrix(zoom, zoom)
        for index, page in enumerate(doc):
            pixmap = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
            rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            # pymupdf renders RGB; the checks built on top of this (both
            # adapted from cv2-based reference scripts) expect OpenCV's
            # native BGR layout.
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            pages.append(
                RenderedPage(
                    page_number=index + 1,
                    image=bgr,
                    width=pixmap.width,
                    height=pixmap.height,
                    has_image_content=len(page.get_images(full=True)) > 0,
                )
            )
    return pages
