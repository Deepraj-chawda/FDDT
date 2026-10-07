"""
Deleted / replaced content ("ghost text") on scans converted to editable text.

When a scan is converted to editable text (Acrobat "Edit scanned document",
iLovePDF and the like), the converter erases the text pixels from the scan
and redraws every word as live text on top of it. The erasure is never
perfect: a faint "ghost" of each word stays in the background image.

  - ghost WITH live text on top of it: normal, that is the converted word;
  - ghost with NO live text on top (ORPHAN): content that was there when
    the page was converted, and has been deleted since;
  - ghost much WIDER than the live text on top (EXTENDED): a line that was
    rewritten shorter ("Registration Fees ......" now reads "Registration
    Fees").

Runs only on pages built that way (`page_structure.PageStructure.
editable_text_over_scan_layout`: a page-sized image drawn first, visible text
drawn over it). A plain scan (no text layer, or only invisible OCR text) and
a born-digital page (no page-sized image) are "not applicable".

How (pixels of the background image, at its own resolution):

  1. ghost enhancement: the paper is estimated with a large median blur; the
     residue is how much darker each pixel is than the paper. Ghosts are the
     FAINT residue band: above the page's noise, below _INK_DIFF (stronger
     is ink still in the image: table rules, logos, stamps, signatures —
     and the anti-aliased halo around it, removed too). Long horizontal and
     vertical rules are removed by morphological opening.
  2. ghost text lines: the faint mask is smeared horizontally into lines,
     and line-shaped components of text height kept.
  3. what may legitimately sit on a ghost: visible text spans, other images
     (the converter lifts logos, stamps and signatures out into their own
     images), filled vector shapes, detected signature/stamp regions, and a
     margin round the page.
  4. a ghost line mostly uncovered is an orphan; ORPHAN lines close together
     are merged into blocks. A ghost line whose uncovered part runs on past
     the end of the text over it, by more than _EXTENDED_SHARE of the last
     word and _EXTENDED_MIN_PT, is extended.
  5. show-through: a page printed on both sides shows the back through the
     paper, mirrored. When an OCR reader is supplied, each orphan block is
     read as is and mirrored; if the mirrored reading is clearly better, the
     block is show-through and not reported as deleted content. The same
     reading is the reviewer's hint of what the deleted text said.

The result keeps every region it found with its page box, an enhanced crop
of it (PNG, base64) for the reviewer, and the hint text; scoring reads the
`ghost_deleted_block` and `ghost_replaced_line` findings.
"""
from __future__ import annotations

import base64
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

import cv2
import numpy as np
import pymupdf

from app.services.forensics.page_structure import analyze_page_structure

# --- gate -------------------------------------------------------------------
# The background image: covers at least this share of the page.
_BACKGROUND_SHARE = 0.9

# --- ghost enhancement --------------------------------------------------------
_PAPER_KERNEL = 0.5  # x median text height: marks thinner than this are strokes
_MIN_GHOST_DIFF = 4  # residue levels; floor of the noise estimate
_INK_DIFF = 70  # residue at or above this is ink still in the image
_INK_HALO_PX = 2  # anti-aliased edge of that ink, removed with it
_MIN_SPECK_PX = 6  # connected components smaller than this are noise
_SHADE_DELTA = 12  # paper this much darker than the page's white is shaded
_RULE_GAP = 0.3  # x median text height: breaks in a faint rule bridged before finding it
_RULE_MAX_THICKNESS = 0.2  # x median text height: a rule is thinner than this
_FLAT_COMPONENT = 0.15  # x median text height: a component this flat (and long) is a rule fragment

# --- ghost lines --------------------------------------------------------------
_LINE_SMEAR = 0.6  # x median text height: horizontal dilation joining glyphs
_LINE_MIN_HEIGHT = 0.5  # x median text height
_LINE_MAX_HEIGHT = 2.5
_LINE_MIN_WIDTH_PT = 25.0
_LINE_MIN_ASPECT = 1.8
_LINE_MIN_DENSITY = 0.03  # ghost pixels / bbox area
_LINE_MAX_DENSITY = 0.60

# --- covering layers ------------------------------------------------------------
_TEXT_PAD_PT = 3.0
_MAX_TEXT_SKEW = 0.1  # sine of the angle
_OVERLAY_PAD_PT = 2.0
_MARGIN_SHARE = 0.04

# --- classification ---------------------------------------------------------------
_ORPHAN_MAX_COVER = 0.20
_EXTENDED_SHARE = 0.30  # of the width of the word at the end of the text
_EXTENDED_MIN_PT = 15.0
_BLOCK_GAP_LINES = 1.5  # orphan lines this close (x line height) form a block
_BLOCK_GAP_COLUMNS = 3.0  # side-by-side orphan lines this close (x line height) join too
_BLOCK_MIN_LINE_PT = 40.0  # a block needs one line at least this wide
# Uncovered ghost that looks like text: its marks fill this share of its
# columns, with at least _MIN_GLYPHS marks this share of the line height tall.
_GLYPH_FILL = 0.5
_GLYPH_HEIGHT = 0.3
_MIN_GLYPHS = 3
# Ghost running on past live text is a fringe beside it: it needs a short
# word's worth of marks.
_MIN_OVERHANG_GLYPHS = 4
# Live words this many text heights from a ghost line still belong to it.
_ANCHOR_REACH = 4.0
# A mark stronger than this x the page's erased text (ghosts under live
# text) is not a ghost: a smudge, or print left in the image.
_MAX_STRENGTH_RATIO = 1.5
_SIGNATURE_OVERLAP = 0.30
# A ghost line with more ink still in the image than this (per ghost pixel)
# is the faint edge of that ink.
_MAX_INK_PER_GHOST = 0.5

# --- show-through ---------------------------------------------------------------------
# Mirrored reading this much better (confidence x characters) is show-through.
_SHOW_THROUGH_RATIO = 1.5
_SHOW_THROUGH_MIN_CHARS = 4
_SHOW_THROUGH_MIN_CONFIDENCE = 0.5

_CROP_PAD_PT = 6.0
_CROP_MAX_WIDTH_PX = 900
_CROP_MIN_HEIGHT_PX = 60  # OCR reads small crops poorly

# An OCR reader for an image (PNG bytes) -> (text, mean word confidence 0-1).
ReadImage = Callable[[bytes], tuple[str, float]]

Box = tuple[float, float, float, float]  # page points, top-left origin


@dataclass
class GhostRegion:
    kind: str  # "orphan" (deleted) | "extended" (replaced) | "show_through"
    page: int
    bbox: Box  # page points
    lines: int = 1
    cover: float = 0.0  # share of the ghost under covering layers
    overhang_pt: float = 0.0  # extended: how far the ghost runs past the text
    text: str = ""  # extended: the live text the ghost runs past
    hint: str = ""  # orphan: OCR reading of the enhanced crop
    hint_confidence: float | None = None
    crop_png: bytes = b""
    line_boxes: list[Box] = field(default_factory=list)


@dataclass
class _Background:
    xref: int
    gray: np.ndarray  # uint8, image pixels
    # pixel -> page points: x = x0 + (u + .5) * sx, y = y0 + (v + .5) * sy
    x0: float
    y0: float
    sx: float
    sy: float

    def to_page(self, u0: float, v0: float, u1: float, v1: float) -> Box:
        return (
            float(self.x0 + u0 * self.sx), float(self.y0 + v0 * self.sy),
            float(self.x0 + u1 * self.sx), float(self.y0 + v1 * self.sy),
        )

    def to_pixels(self, box: Box) -> tuple[int, int, int, int]:
        h, w = self.gray.shape
        u0 = int(np.floor((box[0] - self.x0) / self.sx))
        v0 = int(np.floor((box[1] - self.y0) / self.sy))
        u1 = int(np.ceil((box[2] - self.x0) / self.sx))
        v1 = int(np.ceil((box[3] - self.y0) / self.sy))
        return max(u0, 0), max(v0, 0), min(u1, w), min(v1, h)


def _background(doc: pymupdf.Document, page: pymupdf.Page) -> _Background | None:
    """The page-sized image, as 8-bit gray, with its placement. None when the
    page has none or it is rotated/skewed (not handled)."""
    area = page.rect.get_area() or 1.0
    best = None
    for img in page.get_images(full=True):
        for rect, matrix in page.get_image_rects(img[0], transform=True):
            covered = pymupdf.Rect(rect).intersect(page.rect).get_area()
            if covered >= _BACKGROUND_SHARE * area and (best is None or covered > best[2]):
                best = (img[0], matrix, covered)
    if best is None:
        return None
    xref, m, _ = best
    if abs(m.b) > 1e-3 or abs(m.c) > 1e-3 or m.a == 0 or m.d == 0:
        return None
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is None or pix.colorspace.n != 1 or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, : pix.w].copy()
    if m.a < 0:
        gray = gray[:, ::-1]
    if m.d < 0:
        gray = gray[::-1, :]
    x0, x1 = sorted((m.e, m.e + m.a))
    y0, y1 = sorted((m.f, m.f + m.d))
    return _Background(xref, gray, x0, y0, (x1 - x0) / pix.w, (y1 - y0) / pix.h)


def _residue(gray: np.ndarray, stroke_px: int) -> np.ndarray:
    """How much darker each pixel is than the paper around it, for marks
    thinner than `stroke_px` (morphological black-hat: the paper is the
    image closed with a kernel that size). Glyph strokes — printed or
    ghost — are kept; broad shading (a grey total row, a shaded header)
    and the page's uneven illumination are part of the "paper"."""
    size = max(stroke_px | 1, 9)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)


_NOISE_TILE_PX = 64


def _shaded(gray: np.ndarray, text_px: float) -> np.ndarray:
    """Where the paper itself is not white: shaded cells and bands (often a
    scanner's halftone dots on white), photos, logos left in the scan. Their
    texture looks like faint strokes, and a ghost cannot be told from it, so
    they are not searched. The paper tone is the median over a window of
    about two text heights — text and ghosts are too sparse to move it;
    shaded is _SHADE_DELTA darker than the page's white."""
    size = min(max(int(round(2 * text_px)) | 1, 15), 255)
    tone = cv2.medianBlur(gray, size)
    level = float(np.percentile(tone, 90))
    shaded = (tone < level - _SHADE_DELTA).astype(np.uint8)
    return cv2.dilate(shaded, np.ones((7, 7), np.uint8))


def _noise_floor(diff: np.ndarray, empty: np.ndarray) -> float:
    """The paper's own noise: the 99th percentile of the residue in each
    tile of the page that is empty (no text near it), and the median of
    those over the tiles — so a block of ghosts, which also sits where there
    is no text, raises a few tiles but not the estimate. Floored at
    _MIN_GHOST_DIFF."""
    h, w = diff.shape
    t = _NOISE_TILE_PX
    levels = [
        float(np.percentile(diff[y : y + t, x : x + t], 99))
        for y in range(0, h - t + 1, t)
        for x in range(0, w - t + 1, t)
        if empty[y : y + t, x : x + t].all()
    ]
    if len(levels) < 10:
        return max(_MIN_GHOST_DIFF, float(np.percentile(diff, 95)))
    return max(_MIN_GHOST_DIFF, statistics.median(levels))


def _ghost_mask(diff: np.ndarray, low: float, text_px: float) -> np.ndarray:
    """Faint residue (ghosts), with ink and its halo, ruled lines and specks
    removed. A faint table rule breaks up into short dashes on a scan, so
    rules are found on the residue closed along each direction first, and
    any remaining component much flatter than a glyph is dropped too."""
    h, w = diff.shape
    faint = ((diff >= low) & (diff < _INK_DIFF)).astype(np.uint8)
    ink = (diff >= _INK_DIFF).astype(np.uint8)
    halo = cv2.dilate(ink, np.ones((2 * _INK_HALO_PX + 1, 2 * _INK_HALO_PX + 1), np.uint8))
    marked = (diff >= low).astype(np.uint8)
    gap = max(int(round(_RULE_GAP * text_px)), 3)
    across = cv2.morphologyEx(marked, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 1)))
    down = cv2.morphologyEx(marked, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap)))
    horizontal = cv2.morphologyEx(across, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 20, 10), 1)))
    vertical = cv2.morphologyEx(down, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 30, 10))))
    # A rule is thin; a line of dense glyphs, bridged the same way, is a
    # band as tall as its letters — that is text, not a rule.
    thick = max(int(round(_RULE_MAX_THICKNESS * text_px)), 3)
    horizontal &= 1 - cv2.morphologyEx(horizontal, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, thick)))
    vertical &= 1 - cv2.morphologyEx(vertical, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (thick, 1)))
    rules = cv2.dilate(horizontal | vertical, np.ones((5, 5), np.uint8))
    mask = faint & (1 - halo) & (1 - rules)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    widths, heights = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
    drop = (stats[:, cv2.CC_STAT_AREA] < _MIN_SPECK_PX) | (
        (heights <= max(_FLAT_COMPONENT * text_px, 2)) & (widths >= 3 * heights)
    )
    drop[0] = False
    mask[drop[labels]] = 0
    return mask


def _visible_spans(page: pymupdf.Page) -> list[tuple[Box, str, list[tuple[Box, str]]]]:
    """(bbox, text, words) of every visible horizontal text span."""
    words_by_span: list[tuple[Box, str, list[tuple[Box, str]]]] = []
    words = [(tuple(w[:4]), w[4]) for w in page.get_text("words")]
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            # A converter sets a skewed scan's words skewed too; only text
            # turned well off the horizontal (a side note, a stamp's ring) is left out.
            if abs(line.get("dir", (1, 0))[1]) > _MAX_TEXT_SKEW:
                continue
            for span in line["spans"]:
                if not span["text"].strip() or span.get("alpha", 255) == 0:
                    continue
                box = tuple(span["bbox"])
                inside = [
                    (wb, wt) for wb, wt in words
                    if wb[0] >= box[0] - 1 and wb[2] <= box[2] + 1 and abs((wb[1] + wb[3]) / 2 - (box[1] + box[3]) / 2) < (box[3] - box[1]) / 2
                ]
                words_by_span.append((box, span["text"].strip(), inside))
    return words_by_span


def _pad(box: Box, pad: float) -> Box:
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def _overlap_share(a: Box, b: Box) -> float:
    """Share of `a`'s area inside `b`."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    area = (a[2] - a[0]) * (a[3] - a[1])
    return w * h / area if w > 0 and h > 0 and area > 0 else 0.0


def _enhanced_crop(bg: _Background, box: Box) -> np.ndarray:
    """The scan in `box`, contrast-stretched so its faint traces show as
    dark marks on white (the darkest 0.5% black, the paper's median tone
    white), at least _CROP_MIN_HEIGHT_PX tall for OCR and at most
    _CROP_MAX_WIDTH_PX wide."""
    u0, v0, u1, v1 = bg.to_pixels(_pad(box, _CROP_PAD_PT))
    crop = bg.gray[v0:v1, u0:u1].astype(np.float32)
    if crop.size == 0:
        return np.full((1, 1), 255, np.uint8)
    dark, paper = float(np.percentile(crop, 0.5)), float(np.percentile(crop, 50))
    stretched = np.clip((crop - dark) * (255.0 / max(paper - dark, 1.0)), 0, 255).astype(np.uint8)
    scale = max(_CROP_MIN_HEIGHT_PX / stretched.shape[0], 1.0)
    scale = min(scale, _CROP_MAX_WIDTH_PX / stretched.shape[1])
    if scale != 1.0:
        stretched = cv2.resize(stretched, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    return stretched


def _png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    return buf.tobytes() if ok else b""


@dataclass
class _Line:
    box: Box  # page points, tight around its ghost pixels
    pixels: tuple[int, int, int, int]  # u0, v0, u1, v1
    cover: float  # share of its ghost pixels under covering layers
    ink: float  # ink pixels (still in the image) per ghost pixel, in its box
    strength: float  # 90th percentile residue of its uncovered ghost pixels


def _ghost_lines(bg: _Background, mask: np.ndarray, median_text_px: float) -> list[tuple[Box, tuple[int, int, int, int]]]:
    """Text-line-shaped groups of ghost pixels: (page box, pixel box)."""
    smear = max(int(round(_LINE_SMEAR * median_text_px)), 3)
    joined = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (smear, 3)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    lines = []
    for i in range(1, count):
        x, y, w, h = (int(v) for v in stats[i, :4])
        own = mask[y : y + h, x : x + w] & (labels[y : y + h, x : x + w] == i)
        if not own.any():
            continue
        ys, xs = np.nonzero(own)
        u0, u1, v0, v1 = x + xs.min(), x + xs.max() + 1, y + ys.min(), y + ys.max() + 1
        width_px, height_px = u1 - u0, v1 - v0
        density = own.sum() / float(width_px * height_px)
        box = bg.to_page(u0, v0, u1, v1)
        if not (_LINE_MIN_HEIGHT * median_text_px <= height_px <= _LINE_MAX_HEIGHT * median_text_px):
            continue
        if box[2] - box[0] < _LINE_MIN_WIDTH_PT or width_px / height_px < _LINE_MIN_ASPECT:
            continue
        if not _LINE_MIN_DENSITY <= density <= _LINE_MAX_DENSITY:
            continue
        lines.append((box, (int(u0), int(v0), int(u1), int(v1))))
    return lines


def _text_like(free: np.ndarray, line_height_px: int, sx: float, min_glyphs: int = _MIN_GLYPHS) -> bool:
    """Whether uncovered ghost pixels (one line's rows, some columns) look
    like text rather than scan noise: at least _LINE_MIN_WIDTH_PT of them,
    filling most of their columns, made of several glyph-height marks."""
    cols = free.any(axis=0)
    idx = np.nonzero(cols)[0]
    if idx.size == 0:
        return False
    if (idx.max() - idx.min() + 1) * sx < _LINE_MIN_WIDTH_PT:
        return False
    if cols[idx.min() : idx.max() + 1].mean() < _GLYPH_FILL:
        return False
    _, _, stats, _ = cv2.connectedComponentsWithStats(free.astype(np.uint8), connectivity=8)
    glyphs = int((stats[1:, cv2.CC_STAT_HEIGHT] >= _GLYPH_HEIGHT * line_height_px).sum())
    return glyphs >= min_glyphs


Word = tuple[Box, str, str]  # box, word, the text of the line it is on


def _row_words(line: Box, words: list[Word]) -> list[Word]:
    """Live words on the same row as a ghost line (vertical centre inside it)."""
    pad = 0.3 * (line[3] - line[1])
    return [w for w in words if line[1] - pad <= (w[0][1] + w[0][3]) / 2 <= line[3] + pad]


def _readable(text: str) -> bool:
    """Text the converter could read: a word it could not map to characters
    ("\\x92\\x93: \\x94;:ON-") is set narrower than the print under it, so its
    width says nothing."""
    return not any(unicodedata.category(ch) in ("Cc", "Co", "Cn") for ch in text)


def _overhang(
    line: _Line, row: list[Word], reach: float, free: np.ndarray, bg: _Background
) -> tuple[float, str] | None:
    """(how far, in points, the line's uncovered ghost runs past the live
    text on its row, that text) — for text-like uncovered ghost that starts
    within `reach` of the end (or start) of that text and runs on by more
    than _EXTENDED_SHARE of the word at that end and _EXTENDED_MIN_PT, into
    empty space (not up to the next word on the row). Else None."""
    x0, _, x1, _ = line.box
    anchors = sorted((w for w in row if w[0][2] >= x0 - reach and w[0][0] <= x1 + reach), key=lambda w: w[0][0])
    if not anchors or not all(_readable(w[1]) for w in anchors):
        return None
    u0, v0, u1, v1 = line.pixels
    best: tuple[float, str] | None = None

    last = max(anchors, key=lambda w: w[0][2])
    end = bg.to_pixels(last[0])[2]
    tail = free[v0:v1, max(end, u0) : u1]
    if tail.size and _text_like(tail, v1 - v0, bg.sx, _MIN_OVERHANG_GLYPHS):
        overhang = x1 - last[0][2]
        next_word = min((w[0][0] for w in row if w[0][0] > last[0][2]), default=None)
        if (
            overhang > _EXTENDED_MIN_PT and overhang > _EXTENDED_SHARE * (last[0][2] - last[0][0])
            and (next_word is None or next_word - x1 >= 2 * _TEXT_PAD_PT)
        ):
            best = (overhang, last[2])

    first = min(anchors, key=lambda w: w[0][0])
    start = bg.to_pixels(first[0])[0]
    head = free[v0:v1, u0 : min(start, u1)]
    if head.size and _text_like(head, v1 - v0, bg.sx, _MIN_OVERHANG_GLYPHS):
        overhang = first[0][0] - x0
        prev_word = max((w[0][2] for w in row if w[0][2] < first[0][0]), default=None)
        if (
            overhang > _EXTENDED_MIN_PT and overhang > _EXTENDED_SHARE * (first[0][2] - first[0][0])
            and (prev_word is None or x0 - prev_word >= 2 * _TEXT_PAD_PT)
            and (best is None or overhang > best[0])
        ):
            best = (overhang, first[2])
    return best


def _blocks(orphans: list[_Line], line_height_pt: float) -> list[list[_Line]]:
    """Orphan lines merged into blocks: rows less than _BLOCK_GAP_LINES line
    heights apart, and side by side within _BLOCK_GAP_COLUMNS line heights
    (a column of labels and the column of values beside it)."""
    groups: list[list[_Line]] = []

    def near(line: _Line, group: list[_Line]) -> bool:
        return any(
            max(line.box[1], o.box[1]) - min(line.box[3], o.box[3]) < _BLOCK_GAP_LINES * line_height_pt
            and max(line.box[0], o.box[0]) - min(line.box[2], o.box[2]) < _BLOCK_GAP_COLUMNS * line_height_pt
            for o in group
        )

    for line in sorted(orphans, key=lambda l: (l.box[1], l.box[0])):
        touching = [g for g in groups if near(line, g)]
        merged = [l for g in touching for l in g] + [line]
        groups = [g for g in groups if not any(g is t for t in touching)] + [merged]
    return groups


def _text_rows(lines: list[_Line]) -> int:
    """Rows of text among ghost lines (side-by-side lines are one row)."""
    rows: list[tuple[float, float]] = []
    for line in sorted(lines, key=lambda l: (l.box[1] + l.box[3]) / 2):
        mid = (line.box[1] + line.box[3]) / 2
        if rows and rows[-1][0] <= mid <= rows[-1][1]:
            continue
        rows.append((line.box[1], line.box[3]))
    return len(rows)


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _read_score(reading: tuple[str, float]) -> float:
    text, confidence = reading
    return len("".join(ch for ch in text if ch.isalnum())) * max(confidence, 0.0)


def _show_through(straight: tuple[str, float], mirrored: tuple[str, float]) -> bool:
    """The mirrored crop reads as real text (_SHOW_THROUGH_MIN_CHARS letters
    or digits at _SHOW_THROUGH_MIN_CONFIDENCE) and clearly better than the
    crop as it is."""
    chars = sum(ch.isalnum() for ch in mirrored[0])
    return (
        chars >= _SHOW_THROUGH_MIN_CHARS and mirrored[1] >= _SHOW_THROUGH_MIN_CONFIDENCE
        and _read_score(mirrored) > _SHOW_THROUGH_RATIO * max(_read_score(straight), 1.0)
    )


@dataclass
class PageScan:
    page: int
    regions: list[GhostRegion]
    noise_floor: float = 0.0
    ghost_level: float = 0.0  # 90th percentile residue of the ghosts under live text


def analyze_page(
    doc: pymupdf.Document, page: pymupdf.Page, number: int, *, read_image: ReadImage | None = None
) -> PageScan | None:
    """Ghost regions of one page (module docstring); None when the page has
    no usable page-sized background image."""
    bg = _background(doc, page)
    if bg is None:
        return None
    spans = _visible_spans(page)
    words: list[Word] = [(wb, wt, t) for _, t, ws in spans for wb, wt in ws] or [(b, t, t) for b, t, _ in spans]
    heights = [s[0][3] - s[0][1] for s in spans]
    median_text_pt = statistics.median(heights) if heights else 10.0
    median_text_px = median_text_pt / bg.sy
    diff = _residue(bg.gray, int(round(_PAPER_KERNEL * median_text_px)))

    # What may legitimately sit on a ghost, in image pixels.
    covered = np.zeros(bg.gray.shape, np.uint8)
    under_text = np.zeros(bg.gray.shape, np.uint8)

    def cover(box: Box, target: np.ndarray = covered) -> None:
        u0, v0, u1, v1 = bg.to_pixels(box)
        if u1 > u0 and v1 > v0:
            target[v0:v1, u0:u1] = 1

    for box, _, _ in spans:
        cover(_pad(box, _TEXT_PAD_PT))
        cover(box, under_text)
    for img in page.get_images(full=True):
        if img[0] == bg.xref:
            continue
        for rect in page.get_image_rects(img[0]):
            cover(_pad(tuple(rect), _OVERLAY_PAD_PT))
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        if fill is not None and (drawing.get("fill_opacity") or 1) > 0 and min(fill) < 0.98:
            cover(_pad(tuple(drawing["rect"]), _OVERLAY_PAD_PT))
    mx, my = _MARGIN_SHARE * page.rect.width, _MARGIN_SHARE * page.rect.height
    u0, v0, u1, v1 = bg.to_pixels((page.rect.x0 + mx, page.rect.y0 + my, page.rect.x1 - mx, page.rect.y1 - my))
    margin = np.ones(bg.gray.shape, np.uint8)
    margin[v0:v1, u0:u1] = 0
    covered |= margin
    covered |= _shaded(bg.gray, median_text_px)

    near_text = cv2.dilate(covered, np.ones((15, 15), np.uint8))
    low = _noise_floor(diff, (near_text == 0) & (diff < _INK_DIFF))
    mask = _ghost_mask(diff, low, median_text_px)
    ink = (diff >= _INK_DIFF).astype(np.uint8)
    free = mask & (1 - covered)
    erased = diff[(mask & under_text).astype(bool)]
    ghost_level = float(np.percentile(erased, 90)) if erased.size >= 200 else 0.0

    lines: list[_Line] = []
    for box, px in _ghost_lines(bg, mask, median_text_px):
        a0, b0, a1, b1 = px
        ghost = mask[b0:b1, a0:a1].astype(bool)
        loose = free[b0:b1, a0:a1].astype(bool)
        n = max(int(ghost.sum()), 1)
        strength = float(np.percentile(diff[b0:b1, a0:a1][loose], 90)) if loose.any() else 0.0
        lines.append(_Line(box, px, 1.0 - float(loose.sum()) / n, float(ink[b0:b1, a0:a1].sum()) / n, strength))

    regions: list[GhostRegion] = []
    orphans: list[_Line] = []
    reach = _ANCHOR_REACH * median_text_pt
    for line in lines:
        # Faint edges of ink still in the image (a signature, a stamp, a
        # part of the page the converter left as image) are not ghosts; nor
        # is a mark clearly stronger than this page's erased text (a smudge,
        # print the converter left in the image).
        if line.ink > _MAX_INK_PER_GHOST:
            continue
        if ghost_level and line.strength > _MAX_STRENGTH_RATIO * ghost_level:
            continue
        row = _row_words(line.box, words)
        ext = _overhang(line, row, reach, free, bg)
        if ext is not None:
            regions.append(
                GhostRegion(
                    "extended", number, line.box, cover=round(line.cover, 2), overhang_pt=round(ext[0], 1),
                    text=ext[1], crop_png=_png(_enhanced_crop(bg, line.box)), line_boxes=[line.box],
                )
            )
            continue
        a0, b0, a1, b1 = line.pixels
        if (
            line.cover < _ORPHAN_MAX_COVER
            and not any(w[0][2] >= line.box[0] - reach and w[0][0] <= line.box[2] + reach for w in row)
            and _text_like(free[b0:b1, a0:a1], b1 - b0, bg.sx)
        ):
            orphans.append(line)

    for group in _blocks(orphans, median_text_pt):
        if max(l.box[2] - l.box[0] for l in group) < _BLOCK_MIN_LINE_PT:
            continue
        box = _union([l.box for l in group])
        crop = _enhanced_crop(bg, box)
        region = GhostRegion(
            "orphan", number, box, lines=_text_rows(group),
            cover=round(statistics.fmean(l.cover for l in group), 2),
            crop_png=_png(crop), line_boxes=[l.box for l in group],
        )
        if read_image is not None:
            straight = read_image(_png(crop))
            mirrored = read_image(_png(np.ascontiguousarray(crop[:, ::-1])))
            region.hint, region.hint_confidence = straight[0].strip(), round(straight[1], 2)
            if _show_through(straight, mirrored):
                region.kind = "show_through"
                region.hint, region.hint_confidence = mirrored[0].strip(), round(mirrored[1], 2)
        regions.append(region)
    regions.sort(key=lambda r: (r.bbox[1], r.bbox[0]))
    return PageScan(number, regions, round(low, 1), round(ghost_level, 1))


# --- the check ----------------------------------------------------------------

FINDING_DELETED = "ghost_deleted_block"
FINDING_REPLACED = "ghost_replaced_line"
FINDING_SHOW_THROUGH = "ghost_show_through"
FINDING_NEAR_SIGNATURE = "ghost_near_signature"
FINDING_SCOPE = "ghost_content_scope"


def _shorten(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _normalized(box: Box, page: int, rect: pymupdf.Rect) -> dict[str, Any]:
    width, height = rect.width or 1.0, rect.height or 1.0
    x0, y0 = max(box[0], 0.0), max(box[1], 0.0)
    x1, y1 = min(box[2], width), min(box[3], height)
    return {
        "page": page,
        "x": round(x0 / width, 5),
        "y": round(y0 / height, 5),
        "width": round(max(x1 - x0, 0.0) / width, 5),
        "height": round(max(y1 - y0, 0.0) / height, 5),
    }


def _region_finding(region: GhostRegion, rect: pymupdf.Rect) -> dict[str, Any]:
    box = [round(v, 1) for v in region.bbox]
    data: dict[str, Any] = {
        "kind": region.kind,
        "box_pt": box,
        "crop_png_base64": base64.b64encode(region.crop_png).decode("ascii") if region.crop_png else None,
    }
    where = f"x {box[0]:.0f}–{box[2]:.0f} pt, y {box[1]:.0f}–{box[3]:.0f} pt from the top"
    if region.kind == "extended":
        data.update(text=region.text, overhang_pt=region.overhang_pt)
        finding, severity = FINDING_REPLACED, "medium"
        description = (
            f"Page {region.page}: the faint trace of the original text in the scan runs on {region.overhang_pt:.0f} pt "
            f"past the live text '{_shorten(region.text)}' on its line ({where}). That line was longer when the scan "
            "was converted to editable text, and has been shortened or rewritten since."
        )
    else:
        data.update(lines=region.lines, hint=region.hint or None, hint_confidence=region.hint_confidence if region.hint else None)
        reading = (
            f" OCR of the enhanced trace reads, at best: '{_shorten(region.hint, 80)}'"
            f" (confidence {region.hint_confidence:.2f})." if region.hint else ""
        )
        if region.kind == "show_through":
            finding, severity = FINDING_SHOW_THROUGH, "info"
            description = (
                f"Page {region.page}: faint text in the scan with no live text on top ({where}) reads better "
                f"mirrored, so it is most likely the back of the page showing through the paper. Not scored.{reading}"
            )
        else:
            finding, severity = FINDING_DELETED, "high"
            description = (
                f"Page {region.page}: a block of {region.lines} line(s) of faint text remains in the scanned "
                f"background ({where}) with no live text on top of it: content that was on the page when it was "
                f"converted to editable text, and was deleted afterwards.{reading}"
            )
    return {
        "finding": finding,
        "severity": severity,
        "description": description,
        "page": region.page,
        "bounding_box": _normalized(region.bbox, region.page, rect),
        "data": data,
    }


def _with_result(result: dict[str, Any]) -> dict[str, Any]:
    scored = any(d.get("severity") in ("medium", "high") for d in result["details"])
    return {**result, "result": "flag" if scored else "pass"}


def _not_applicable(description: str, reason: str) -> dict[str, Any]:
    return {
        "result": "not_applicable",
        "details": [
            {"finding": FINDING_SCOPE, "severity": "info", "description": description,
             "data": {"pages": [], "reason": reason}}
        ],
    }


def _why_not_converted(structure: list) -> str:
    """Why no page is a converted scan, in a few words."""
    scanned = [p for p in structure if p.background_image]
    if not scanned:
        return "no scanned page (a born-digital file)"
    if any(p.invisible_runs_over_image for p in scanned) and not any(p.visible_runs_over_image for p in scanned):
        return "the text layer is invisible OCR text (a searchable scan)"
    if not any(p.visible_runs_over_image for p in scanned):
        return "no text layer (a plain scan)"
    return "too little visible text over the scan"


def analyze_ghost_content(pdf_bytes: bytes, read_image: ReadImage | None = None) -> dict[str, Any]:
    """The check over one PDF: {"result": "flag"|"pass"|"not_applicable",
    "details": [finding, ...]} for a `document_checks.result` column. Only
    pages that are a scan converted to editable text are searched (module
    docstring); with none, the result is "not_applicable". `read_image`
    (optional) reads an image crop, for the show-through test and the
    reviewer's hint; without it neither is done."""
    structure = analyze_page_structure(pdf_bytes)
    pages = [p.page for p in structure if p.vector_text_over_image]
    if not pages:
        why = _why_not_converted(structure)
        return _not_applicable(
            f"Not applicable: {why}. Only a scan converted to editable text (a page-sized scanned image with "
            "visible live text drawn over it) has an erased background to search.",
            why,
        )
    findings: list[dict[str, Any]] = []
    searched: list[dict[str, Any]] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for number in pages:
            page = doc[number - 1]
            scan = analyze_page(doc, page, number, read_image=read_image)
            if scan is None:
                continue
            searched.append({"page": number, "noise_floor": scan.noise_floor, "ghost_level": scan.ghost_level})
            findings.extend(_region_finding(r, page.rect) for r in scan.regions)
    finally:
        doc.close()
    if not searched:
        return _not_applicable(
            "Not applicable: the converted page(s) place their scanned image rotated or skewed, which this check "
            "does not handle; the background was not searched.",
            "the scan is placed rotated",
        )
    listed = ", ".join(str(p["page"]) for p in searched)
    findings.insert(
        0,
        {
            "finding": FINDING_SCOPE,
            "severity": "info",
            "description": (
                f"Page(s) {listed}: a scan converted to editable text. Its background image was searched for the "
                "faint traces the conversion leaves of the original text, and each trace compared with the live "
                "text on top of it."
                + ("" if read_image else " The traces were not read with OCR (no reader available), so there is no "
                   "show-through test or text hint.")
            ),
            "data": {"pages": searched, "ocr_hint": read_image is not None},
        },
    )
    return _with_result({"details": findings})


def exclude_signature_regions(result: dict[str, Any], detected: list[dict[str, Any]] | None) -> dict[str, Any]:
    """`result` with each ghost finding that lies _SIGNATURE_OVERLAP or more
    inside a detected signature or stamp region set aside (info, not
    scored): the faint parts of a signature's or a stamp's ink are not
    deleted text. Applied whenever the detection is, or becomes, available."""
    if not detected or result.get("result") == "not_applicable":
        return result
    regions = []
    for d in detected:
        box = d.get("bounding_box") if isinstance(d, dict) else None
        if isinstance(box, dict) and {"x", "y", "width", "height"} <= box.keys():
            regions.append(
                (int(box.get("page") or 1), (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]),
                 d.get("kind") or "signature")
            )
    changed = False
    details = []
    for f in result.get("details") or []:
        box = f.get("bounding_box") if isinstance(f, dict) else None
        if isinstance(box, dict) and f.get("finding") in (FINDING_DELETED, FINDING_REPLACED):
            area = (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
            hit = next(
                (r for r in regions if r[0] == box.get("page") and _overlap_share(area, r[1]) >= _SIGNATURE_OVERLAP),
                None,
            )
            if hit is not None:
                f = {
                    **f,
                    "finding": FINDING_NEAR_SIGNATURE,
                    "severity": "info",
                    "description": f"Set aside, not scored: it lies over the detected {hit[2]}. " + f["description"],
                    "data": {**(f.get("data") or {}), "set_aside_finding": f["finding"]},
                }
                changed = True
        details.append(f)
    return _with_result({**result, "details": details}) if changed else result


def _page_with_box(pdf_bytes: bytes, page_number: int, box: list[float]) -> bytes:
    """The page as a small layout thumbnail (too small to read — the
    model must not take the words around the block for the block's own),
    the erased block filled in teal: where on the page it was."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_number - 1]
        shape = page.new_shape()
        shape.draw_rect(pymupdf.Rect(*box))
        shape.finish(color=(0.05, 0.58, 0.53), fill=(0.05, 0.58, 0.53), width=2)
        shape.commit()
        return page.get_pixmap(dpi=24).tobytes("png")
    finally:
        doc.close()


_VISION_RUNS = 3
# Words that say nothing about what the block was.
_FILLER_WORDS = {"the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "is", "by", "at", "as", "be"}


def _agreed_hint(answers: list) -> dict[str, Any]:
    """What the vision model's answers agree on. It is asked several times
    because on a trace this faint it guesses — differently each time; a word
    or heading only counts when at least two answers give it. Kinds are all
    kept (the reviewer sees whether they agree); confidence is "medium" only
    when the answers agree on the kind and read something consistently,
    else "low"."""
    def norm(text: str) -> str:
        return " ".join(text.lower().strip(" .:;,-").split())

    words: dict[str, int] = {}
    shown: dict[str, str] = {}
    for answer in answers:
        for w in {norm(w): w.strip() for w in answer.legible_words if norm(w)}.items():
            words[w[0]] = words.get(w[0], 0) + 1
            shown.setdefault(w[0], w[1])
    headings: dict[str, int] = {}
    for answer in answers:
        if norm(answer.heading):
            headings[norm(answer.heading)] = headings.get(norm(answer.heading), 0) + 1
    agreed_words = [
        shown[w] for w, n in words.items() if n >= 2 and any(c.isalnum() for c in w) and w not in _FILLER_WORDS
    ]
    heading = next((h for h, n in headings.items() if n >= 2), "")
    kinds = list(dict.fromkeys(a.kind.strip() for a in answers if a.kind.strip()))
    consistent = bool(agreed_words or heading)
    return {
        "kind": kinds[0] if len(kinds) == 1 else " / ".join(kinds),
        "kinds": kinds,
        "heading": next((a.heading.strip() for a in answers if norm(a.heading) == heading), "") if heading else "",
        "legible_words": agreed_words[:20],
        # Never "high": a trace this faint is never read for certain.
        "confidence": "medium" if consistent and len(kinds) <= 1 else "low",
        "runs": len(answers),
        "answers_agree": len(kinds) <= 1,
    }


def add_vision_hints(result: dict[str, Any], pdf_bytes: bytes, guess) -> dict[str, Any]:
    """Each deleted block's `data.vision_hint`: what a vision model, asked
    _VISION_RUNS times, consistently makes of the erased text
    (`guess(trace_data_uri, page_data_uri)` returns kind / heading /
    legible_words / confidence). A hint for the reviewer — not scored, not
    evidence. A failed guess is no guess."""
    for finding in result.get("details") or []:
        data = finding.get("data") if isinstance(finding, dict) else None
        if finding.get("finding") != FINDING_DELETED or not isinstance(data, dict) or not data.get("crop_png_base64"):
            continue
        try:
            page_png = _page_with_box(pdf_bytes, int(finding.get("page") or 1), data["box_pt"])
            trace_uri = "data:image/png;base64," + data["crop_png_base64"]
            page_uri = "data:image/png;base64," + base64.b64encode(page_png).decode("ascii")
            answers = [guess(trace_uri, page_uri) for _ in range(_VISION_RUNS)]
        except Exception:  # noqa: BLE001 - best effort
            continue
        data["vision_hint"] = _agreed_hint(answers)
    return result
