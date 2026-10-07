"""
How each PDF page is built: whether a scanned image is its background, and
whether text sits on top of that image as visible, live (editable) text.

A plain scan is one page-sized image. A searchable scan adds the OCR text on
top in render mode 3 (invisible): selectable, not drawn. A scan converted to
editable text (Acrobat "Edit scanned document" and similar tools) instead
draws every word as visible live text over a background image with the
original text wiped — which is what lets its numbers be retyped like a
word-processor file. Drawing order matters: an image drawn AFTER the text
(a flattened page pasted over it) hides that text rather than sitting behind
it, and is not this structure.

Read from PyMuPDF's bbox log, which lists what the page draws in order:
"fill-image" (the image), "fill-text"/"stroke-text" (visible text) and
"ignore-text" (invisible text, render mode 3).

Used by:
  - metadata forensics (`editable_text_over_scan`, medium): a page-sized
    background image under visible text covering most of the page, whatever
    the fonts are called — a converter's generated "-NNNN" subsets, or the
    ordinary "ABCDEF+" subsets Acrobat's own editor leaves. Evidence that
    does not depend on the XMP history, so it still holds when the metadata
    is stripped.
  - the ghost-content check (app/services/forensics/ghost_content.py) runs
    only on such pages.
  - the image-tampering task: error level analysis and copy-move only see
    pixels, so on such a page they cannot see a text edit, and report
    "limited" instead of "pass" (`limit_pixel_check`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymupdf

from app.services.forensics.font_consistency import is_ocr_subset_font

# An image covering at least this share of the page is its background (a
# scan; it may overhang the page edges, as converters place it).
_BACKGROUND_IMAGE_SHARE = 0.9
# Visible text over the image needs at least this many text runs, spanning
# at least this share of the page's height ("covering most of the page").
_MIN_TEXT_RUNS = 15
_MIN_TEXT_HEIGHT_SHARE = 0.5
# Of the text drawn over the image, at least this share must be visible.
_VISIBLE_SHARE = 0.8
# Of the visible characters, at least this share in converter-generated fonts
# makes the page a scan converted to editable text.
_CONVERTER_FONT_SHARE = 0.6

_VISIBLE_TEXT = {"fill-text", "stroke-text"}
_INVISIBLE_TEXT = "ignore-text"
_INVISIBLE_RENDER_MODE = 3


@dataclass
class PageStructure:
    page: int  # 1-based
    background_image: bool  # a page-sized image; text drawn after it is over it
    visible_runs_over_image: int
    invisible_runs_over_image: int
    text_height_share: float  # vertical span of that visible text / page height
    visible_chars: int
    converter_font_chars: int

    @property
    def vector_text_over_image(self) -> bool:
        total = self.visible_runs_over_image + self.invisible_runs_over_image
        return (
            self.background_image
            and self.visible_runs_over_image >= _MIN_TEXT_RUNS
            and self.visible_runs_over_image >= _VISIBLE_SHARE * total
            and self.text_height_share >= _MIN_TEXT_HEIGHT_SHARE
        )

    @property
    def converter_fonts(self) -> bool:
        return self.visible_chars > 0 and self.converter_font_chars >= _CONVERTER_FONT_SHARE * self.visible_chars

    @property
    def editable_text_over_scan(self) -> bool:
        return self.vector_text_over_image and self.converter_fonts


def _vertical_span(boxes: list[pymupdf.Rect]) -> float:
    """From the top of the highest text to the bottom of the lowest."""
    return max(b.y1 for b in boxes) - min(b.y0 for b in boxes) if boxes else 0.0


def _page_structure(page: pymupdf.Page, number: int) -> PageStructure:
    area = page.rect.get_area() or 1.0
    log = page.get_bboxlog()
    first_background = next(
        (
            i for i, (kind, rect) in enumerate(log)
            if kind == "fill-image"
            and pymupdf.Rect(rect).intersect(page.rect).get_area() >= _BACKGROUND_IMAGE_SHARE * area
        ),
        None,
    )
    visible_boxes: list[pymupdf.Rect] = []
    invisible = 0
    if first_background is not None:
        for kind, rect in log[first_background + 1 :]:
            if kind in _VISIBLE_TEXT:
                visible_boxes.append(pymupdf.Rect(rect))
            elif kind == _INVISIBLE_TEXT:
                invisible += 1
    visible_chars = converter = 0
    for span in page.get_texttrace():
        if span["type"] == _INVISIBLE_RENDER_MODE or span.get("opacity", 1) == 0:
            continue
        n = sum(1 for c in span["chars"] if chr(c[0]).strip())
        visible_chars += n
        if is_ocr_subset_font(span["font"]):
            converter += n
    return PageStructure(
        page=number,
        background_image=first_background is not None,
        visible_runs_over_image=len(visible_boxes),
        invisible_runs_over_image=invisible,
        text_height_share=_vertical_span(visible_boxes) / (page.rect.height or 1.0),
        visible_chars=visible_chars,
        converter_font_chars=converter,
    )


def analyze_page_structure(pdf_bytes: bytes) -> list[PageStructure]:
    """One PageStructure per page; empty for a file PyMuPDF cannot read."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 - other checks report unreadable files
        return []
    try:
        if doc.needs_pass:
            return []
        return [_page_structure(page, index + 1) for index, page in enumerate(doc)]
    finally:
        doc.close()


def limit_pixel_check(result: dict[str, Any], structure: list[PageStructure], check_label: str) -> dict[str, Any]:
    """`result` of a pixel-level check (ELA, copy-move), reported as
    "limited" rather than "pass" when some page's text is vector text drawn
    over an image. A flag stands: what the check found is still evidence."""
    pages = [p.page for p in structure if p.vector_text_over_image]
    if not pages or result.get("result") != "pass":
        return result
    listed = ", ".join(str(p) for p in pages)
    note = {
        "finding": "pixel_analysis_limited",
        "severity": "info",
        "description": (
            f"Limited: the text on page(s) {listed} is vector text drawn over an image, so {check_label}, "
            "which looks only at pixels, cannot see an edit to that text. Nothing was found in the image itself."
        ),
        "data": {"pages": pages},
    }
    return {**result, "result": "limited", "details": [note, *(result.get("details") or [])]}
