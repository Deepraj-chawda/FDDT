"""
Small facts about how a PDF was made, read with PyMuPDF and shared by
several checks:

  - raster_image_count: how many raster images the pages draw. A "Scanned
    with CamScanner" line on a file with none is typed text, not a scan
    (field validation, `fake_scan_watermark`).
  - region_material: what a detected signature/stamp region is made of —
    "image" (an embedded picture of it), "scan" (part of a scanned page),
    or "text" / "vector" / "text_and_vector" when it is live text and drawn
    lines: a stamp typed into the document, not an ink stamp
    (`stamp_authenticity`).
  - css_palette: colours of the page that belong to a web CSS framework's
    stock palette (Bootstrap 5, Tailwind) — a page built as HTML and printed
    from a browser (metadata forensics, `web_page_origin`).
"""
from __future__ import annotations

from typing import Any

import pymupdf

# An image covering this share of the page is the page itself (a scan).
_PAGE_IMAGE_SHARE = 0.8
# An image covering this share of a region is a picture of it.
_REGION_IMAGE_SHARE = 0.3

# Stock colours of CSS frameworks a page builder uses as-is (exact hex).
CSS_PALETTES: dict[str, set[str]] = {
    "Bootstrap 5": {
        "#0d6efd", "#6c757d", "#198754", "#dc3545", "#ffc107", "#0dcaf0", "#d63384", "#6610f2", "#6f42c1",
        "#fd7e14", "#20c997", "#212529", "#495057", "#adb5bd", "#ced4da", "#dee2e6", "#e9ecef", "#f8f9fa",
        "#664d03", "#055160", "#0f5132", "#842029", "#084298", "#fff3cd", "#cff4fc", "#d1e7dd", "#f8d7da",
        "#cfe2ff", "#e2e3e5", "#41464b", "#ffecb5", "#badbcc", "#f5c2c7", "#b6d4fe",
    },
    "Tailwind": {
        "#3b82f6", "#2563eb", "#1d4ed8", "#ef4444", "#dc2626", "#10b981", "#059669", "#22c55e", "#16a34a",
        "#f59e0b", "#d97706", "#6b7280", "#4b5563", "#374151", "#1f2937", "#111827", "#9ca3af", "#d1d5db",
        "#e5e7eb", "#f3f4f6", "#ec4899", "#8b5cf6", "#6366f1", "#0ea5e9", "#14b8a6", "#64748b", "#475569",
        "#334155", "#1e293b", "#0f172a", "#e2e8f0", "#f1f5f9", "#cbd5e1",
    },
}
# Distinct stock colours of one framework that make the page look built
# from it (one or two can be coincidence).
CSS_PALETTE_MIN_COLOURS = 3


def _open(pdf_bytes: bytes) -> pymupdf.Document | None:
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001
        return None
    if doc.needs_pass:
        doc.close()
        return None
    return doc


def raster_image_count(pdf_bytes: bytes) -> int | None:
    """Raster images drawn on the pages (each placement counted); None if
    the file cannot be read."""
    doc = _open(pdf_bytes)
    if doc is None:
        return None
    try:
        return sum(len(page.get_image_rects(img[0])) for page in doc for img in page.get_images(full=True))
    finally:
        doc.close()


def _region_rect(page: pymupdf.Page, box: dict[str, Any]) -> pymupdf.Rect:
    r = page.rect
    return pymupdf.Rect(
        r.x0 + box["x"] * r.width, r.y0 + box["y"] * r.height,
        r.x0 + (box["x"] + box["width"]) * r.width, r.y0 + (box["y"] + box["height"]) * r.height,
    )


def region_material(doc: pymupdf.Document, box: dict[str, Any]) -> str:
    """What the page draws inside a normalized region (module docstring)."""
    page_number = int(box.get("page") or 1)
    if not 1 <= page_number <= doc.page_count:
        return "unknown"
    page = doc[page_number - 1]
    region = _region_rect(page, box)
    area = region.get_area() or 1.0
    page_area = page.rect.get_area() or 1.0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            covered = pymupdf.Rect(rect).intersect(region).get_area()
            if covered >= _REGION_IMAGE_SHARE * area:
                return "scan" if pymupdf.Rect(rect).get_area() >= _PAGE_IMAGE_SHARE * page_area else "image"
    text = page.get_text("text", clip=region).strip()
    vectors = [d for d in page.get_drawings() if pymupdf.Rect(d["rect"]).intersects(region)]
    if text and vectors:
        return "text_and_vector"
    if text:
        return "text"
    if vectors:
        return "vector"
    return "none"


def annotate_region_materials(detected: list[dict[str, Any]], pdf_bytes: bytes | None) -> list[dict[str, Any]]:
    """Each detected signature/stamp region with its `material` (best effort:
    unchanged when the file cannot be read)."""
    doc = _open(pdf_bytes) if pdf_bytes else None
    if doc is None:
        return detected
    try:
        out = []
        for region in detected:
            box = region.get("bounding_box") if isinstance(region, dict) else None
            if isinstance(box, dict) and {"x", "y", "width", "height"} <= box.keys():
                region = {**region, "material": region_material(doc, box)}
            out.append(region)
        return out
    finally:
        doc.close()


def _hex(colour: Any) -> str | None:
    if colour is None:
        return None
    if isinstance(colour, int):
        return f"#{colour:06x}"
    if isinstance(colour, (tuple, list)) and len(colour) == 3:
        return "#" + "".join(f"{round(c * 255):02x}" for c in colour)
    return None


def css_palette(pdf_bytes: bytes) -> tuple[str | None, list[str]]:
    """(framework, its stock colours used on the pages) for the framework
    with the most, when at least CSS_PALETTE_MIN_COLOURS; else (None, [])."""
    doc = _open(pdf_bytes)
    if doc is None:
        return None, []
    used: set[str] = set()
    try:
        for page in doc:
            for drawing in page.get_drawings():
                used.update(c for c in (_hex(drawing.get("fill")), _hex(drawing.get("color"))) if c)
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    used.update(c for c in (_hex(span.get("color")) for span in line["spans"]) if c)
    finally:
        doc.close()
    best = max(CSS_PALETTES, key=lambda name: len(CSS_PALETTES[name] & used))
    hits = sorted(CSS_PALETTES[best] & used)
    return (best, hits) if len(hits) >= CSS_PALETTE_MIN_COLOURS else (None, [])
