"""
Automatic signature/stamp detection (SPECIFICATION.md §2.3, Azure OpenAI vision).

Runs on every uploaded PDF. It answers only two questions per page —
"is there a signature/stamp mark here, and where?" and "would a document
like this normally carry one?" — so the result can flag *expected-but-
missing* while treating mere presence as a pass.

This is a presence/placement aid for a human reviewer, NOT an identity or
authenticity check, and nothing here compares one signature to another
(that only happens after a reviewer explicitly creates a reference — see
app/tasks/signature_comparison_task.py).

Result shape (stored in `document_checks.result`):
    {
      "result": "pass" | "flag",
      "details": {
        "signature_expected": bool,
        "detected": [{kind, description, text?, confidence, bounding_box{page,x,y,width,height}}],
        "bounding_box": {page,x,y,width,height} | None,   # primary region
      }
    }
`details.bounding_box` is the region the comparison task crops.

Box accuracy: the vision model's coordinates are only approximate (observed
off by 0.03-0.1 of the page — enough to crop the printed name under a
signature instead of the signature itself), so every model box is refined
before it is stored, and each detected entry records how in `refinement`:
  1. "embedded_image" — the page has an embedded raster image (the usual
     case for a pasted/scanned signature or stamp) sitting where the model
     pointed; its exact placement rectangle replaces the model's box.
  2. "second_look" — otherwise the model is shown a zoomed window around its
     own first box and asked again, which localises far more accurately than
     a whole-page view.
  3. "none" — neither worked; the (padded) first-pass box is kept.
Both refined kinds are then trimmed to the ink inside them.

Each entry also records its `material` (app/services/forensics/pdf_facts.py):
"image", "scan", or "text" / "vector" / "text_and_vector" — a stamp drawn
as live text and lines in a born-digital file, not an ink stamp.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

import cv2
import numpy as np
import pymupdf

from app.services.forensics.pdf_facts import annotate_region_materials
from app.services.forensics.pdf_render import RenderedPage
from app.services.llm_service import LLMService
from app.services.visual_inconsistency_service import _encode_page_image

# Bounds vision-model cost on very long documents. Signatures are
# overwhelmingly on the first or last pages, so this covers real cases.
MAX_PAGES_ANALYZED = 10

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


# Breathing room added around a model-reported box: a tight box that clips
# the strokes of a signature makes for a poor comparison crop.
_PAD_X = 0.02
_PAD_Y = 0.03

# Padding for boxes refined against real pixels — already tight, so only a hair
# of margin (a wide pad drags the printed name/heading into the crop).
_REFINED_PAD = 0.004

# An embedded image is only taken as "the signature/stamp" if it touches the
# model's box grown by this margin, and isn't page-sized (a full-page scan).
_SNAP_MARGIN = 0.06
_SNAP_MAX_PAGE_FRACTION = 0.35

# Zoomed window for the second look: the model's box grown by this much per side.
_WINDOW_PAD_X = 0.10
_WINDOW_PAD_Y = 0.08

# Pixels darker than this count as ink when trimming/checking a crop.
_INK_GRAY_MAX = 215
_MIN_INK_RATIO = 0.002

logger = logging.getLogger(__name__)


def _clamp_box(
    box: dict[str, float], pad_x: float = _PAD_X, pad_y: float = _PAD_Y
) -> dict[str, float] | None:
    """Model-supplied boxes can spill past the page edge; clamp to 0-1, pad a
    little, and drop anything degenerate so a bad box never reaches the
    cropper."""
    x = min(max(float(box["x"]), 0.0), 1.0)
    y = min(max(float(box["y"]), 0.0), 1.0)
    width = min(float(box["width"]), 1.0 - x)
    height = min(float(box["height"]), 1.0 - y)
    if width <= 0.005 or height <= 0.005:
        return None
    x2 = min(1.0, x + width + pad_x)
    y2 = min(1.0, y + height + pad_y)
    x = max(0.0, x - pad_x)
    y = max(0.0, y - pad_y)
    return {"x": x, "y": y, "width": x2 - x, "height": y2 - y}


def _crop(image: np.ndarray, box: dict[str, float]) -> np.ndarray:
    height, width = image.shape[:2]
    x1, y1 = int(box["x"] * width), int(box["y"] * height)
    x2, y2 = int((box["x"] + box["width"]) * width), int((box["y"] + box["height"]) * height)
    return image[y1 : max(y2, y1 + 1), x1 : max(x2, x1 + 1)]


def _ink_mask(crop: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return gray < _INK_GRAY_MAX


def _has_ink(image: np.ndarray, box: dict[str, float]) -> bool:
    mask = _ink_mask(_crop(image, box))
    return mask.size > 0 and float(mask.mean()) >= _MIN_INK_RATIO


def _trim_to_ink(image: np.ndarray, box: dict[str, float]) -> dict[str, float]:
    """Shrink `box` to the ink inside it (plus a hair of margin) so blank
    margins don't dilute the comparison crop. Returns `box` unchanged if
    there is no ink to anchor on."""
    mask = _ink_mask(_crop(image, box))
    if mask.size == 0 or not mask.any():
        return box
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    mh, mw = mask.shape
    trimmed = {
        "x": box["x"] + box["width"] * cols[0] / mw,
        "y": box["y"] + box["height"] * rows[0] / mh,
        "width": box["width"] * (cols[-1] + 1 - cols[0]) / mw,
        "height": box["height"] * (rows[-1] + 1 - rows[0]) / mh,
    }
    return _clamp_box(trimmed, _REFINED_PAD, _REFINED_PAD) or box


def _embedded_image_rects(pdf_bytes: bytes | None) -> dict[int, list[dict[str, float]]]:
    """Normalized placement rectangle of every embedded raster image, per
    1-based page. Empty if the bytes aren't a readable PDF; rotated pages are
    skipped (their image coordinates would need un-rotating and the
    second-look fallback handles them fine)."""
    if not pdf_bytes:
        return {}
    rects: dict[int, list[dict[str, float]]] = {}
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                if page.rotation:
                    continue
                page_w, page_h = page.rect.width, page.rect.height
                for info in page.get_image_info():
                    r = pymupdf.Rect(info["bbox"]) & page.rect
                    if r.is_empty or r.width < 0.01 * page_w or r.height < 0.01 * page_h:
                        continue
                    if r.width * r.height > _SNAP_MAX_PAGE_FRACTION * page_w * page_h:
                        continue
                    rects.setdefault(index + 1, []).append(
                        {
                            "x": (r.x0 - page.rect.x0) / page_w,
                            "y": (r.y0 - page.rect.y0) / page_h,
                            "width": r.width / page_w,
                            "height": r.height / page_h,
                        }
                    )
    except Exception:  # noqa: BLE001 - refinement is best-effort, never fail detection
        logger.warning("embedded-image lookup failed", exc_info=True)
        return {}
    return rects


def _center(box: dict[str, float]) -> tuple[float, float]:
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _touches(box: dict[str, float], other: dict[str, float], margin: float) -> bool:
    return (
        box["x"] < other["x"] + other["width"] + margin
        and other["x"] < box["x"] + box["width"] + margin
        and box["y"] < other["y"] + other["height"] + margin
        and other["y"] < box["y"] + box["height"] + margin
    )


def _snap_to_embedded_images(
    boxes: list[dict[str, float]], rects: list[dict[str, float]]
) -> dict[int, dict[str, float]]:
    """Assign each model box (by index) at most one embedded image, each image
    to at most one box, nearest centers first — so a page carrying both a
    signature and a stamp doesn't send both regions to the same image."""
    pairs = sorted(
        (
            (
                (_center(box)[0] - _center(rect)[0]) ** 2
                + (_center(box)[1] - _center(rect)[1]) ** 2,
                i,
                j,
            )
            for i, box in enumerate(boxes)
            for j, rect in enumerate(rects)
            if _touches(box, rect, _SNAP_MARGIN)
        )
    )
    snapped: dict[int, dict[str, float]] = {}
    used: set[int] = set()
    for _dist, i, j in pairs:
        if i in snapped or j in used:
            continue
        snapped[i] = rects[j]
        used.add(j)
    return snapped


def _search_window(box: dict[str, float]) -> dict[str, float]:
    """The model's box grown on every side — where the second look searches."""
    x1 = max(0.0, box["x"] - _WINDOW_PAD_X)
    y1 = max(0.0, box["y"] - _WINDOW_PAD_Y)
    x2 = min(1.0, box["x"] + box["width"] + _WINDOW_PAD_X)
    y2 = min(1.0, box["y"] + box["height"] + _WINDOW_PAD_Y)
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


def _second_look(
    llm: LLMService, page: RenderedPage, window: dict[str, float], kind: str
) -> dict[str, float] | None:
    """Re-ask the model about the zoomed `window` (see `_search_window`) and
    map its (far more accurate) answer back to full-page coordinates. Returns
    None if the model finds nothing of `kind` in the window or the call fails."""
    crop = _crop(page.image, window)
    sub_page = dataclasses.replace(page, image=crop, width=crop.shape[1], height=crop.shape[0])
    try:
        analysis = llm.detect_signatures_stamps(_encode_page_image(_with_reference_grid(sub_page)))
    except Exception:  # noqa: BLE001 - keep the first-pass box rather than fail detection
        logger.warning("signature second-look failed", exc_info=True)
        return None
    candidates = [r for r in analysis.regions if r.kind == kind]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: _CONFIDENCE_RANK[r.confidence])
    inner = _clamp_box(best.bounding_box.model_dump(), 0.0, 0.0)
    if inner is None:
        return None
    return {
        "x": window["x"] + inner["x"] * window["width"],
        "y": window["y"] + inner["y"] * window["height"],
        "width": inner["width"] * window["width"],
        "height": inner["height"] * window["height"],
    }


def _with_reference_grid(page: RenderedPage) -> RenderedPage:
    """A copy of `page` with a light labeled grid (every 0.1 of width/height).

    Vision models read a page's content well but are poor at estimating
    *where* something is as a fraction of the page — observed boxes off by
    ~0.3 of the page height. Visible labeled gridlines let the model read
    coordinates off the image instead of guessing them. The grid only
    exists in the copy sent to the model; the stored/rendered page is
    untouched and returned boxes stay normalized to the original page."""
    image = page.image.copy()
    height, width = image.shape[:2]
    overlay = image.copy()
    colour = (0, 0, 255)  # BGR red — distinct from typical black/blue ink
    font_scale = max(0.5, min(width, height) / 1400)
    thickness = max(1, round(font_scale * 2))
    for i in range(1, 10):
        fx = round(width * i / 10)
        fy = round(height * i / 10)
        cv2.line(overlay, (fx, 0), (fx, height), colour, 1)
        cv2.line(overlay, (0, fy), (width, fy), colour, 1)
    image = cv2.addWeighted(overlay, 0.35, image, 0.65, 0)
    for i in range(1, 10):
        label = f"{i / 10:.1f}"
        fx = round(width * i / 10)
        fy = round(height * i / 10)
        cv2.putText(image, label, (fx + 3, round(18 * font_scale) + 4), cv2.FONT_HERSHEY_SIMPLEX, font_scale, colour, thickness)
        cv2.putText(image, label, (4, fy - 4), cv2.FONT_HERSHEY_SIMPLEX, font_scale, colour, thickness)
    return dataclasses.replace(page, image=image)


def detect_signatures(
    llm: LLMService, pages: list[RenderedPage], pdf_bytes: bytes | None = None
) -> dict[str, Any]:
    """`pdf_bytes` (the source file) enables the embedded-image refinement;
    without it every region falls back to the second look."""
    signature_expected = False
    detected: list[dict[str, Any]] = []
    embedded = _embedded_image_rects(pdf_bytes)

    for page in pages[:MAX_PAGES_ANALYZED]:
        analysis = llm.detect_signatures_stamps(_encode_page_image(_with_reference_grid(page)))
        signature_expected = signature_expected or analysis.signature_expected

        located = []
        for region in analysis.regions:
            raw = _clamp_box(region.bounding_box.model_dump(), 0.0, 0.0)
            if raw is not None:
                located.append((region, raw))
        snapped = _snap_to_embedded_images(
            [raw for _region, raw in located], embedded.get(page.page_number, [])
        )

        for index, (region, raw) in enumerate(located):
            refinement = "none"
            box = None
            if index in snapped:
                refinement, box = "embedded_image", snapped[index]
            else:
                # Gate on ink in the whole search window, not just the box: a
                # box sitting on whitespace next to the real mark still needs
                # the second look, while a genuinely blank area has nothing
                # to relocate.
                window = _search_window(raw)
                if _has_ink(page.image, window):
                    box = _second_look(llm, page, window, region.kind)
                    if box is not None:
                        refinement = "second_look"

            if box is not None:
                box = _clamp_box(_trim_to_ink(page.image, box), _REFINED_PAD, _REFINED_PAD)
            if box is None:
                refinement = "none"
                box = _clamp_box(raw)
            if box is None:
                continue
            detected.append(
                {
                    "kind": region.kind,
                    "description": region.description,
                    **({"text": region.text.strip()} if region.kind == "stamp" and region.text.strip() else {}),
                    "confidence": region.confidence,
                    "refinement": refinement,
                    "bounding_box": {"page": page.page_number, **box},
                }
            )

    # What each region is made of (an embedded image, part of a scanned
    # page, or live text and vector lines) — field validation's
    # stamp_authenticity reads it.
    detected = annotate_region_materials(detected, pdf_bytes)

    primary = (
        max(
            detected,
            key=lambda d: (
                _CONFIDENCE_RANK[d["confidence"]],
                d["kind"] == "signature",
            ),
        )
        if detected
        else None
    )

    return {
        # Presence alone is a pass; only expected-but-missing flags.
        "result": "flag" if signature_expected and not detected else "pass",
        "details": {
            "signature_expected": signature_expected,
            "detected": detected,
            "bounding_box": primary["bounding_box"] if primary else None,
        },
    }
