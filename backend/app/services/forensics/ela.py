"""
Error Level Analysis (SPECIFICATION.md section 3.2 "Tampering/alteration
detection"). Detects localized JPEG-recompression-error anomalies that
can indicate a region of a page was edited after the rest of it was
last saved/scanned.

Adapted from an internal reference tool (`error_level.py` — an
interactive quality/scale/contrast-slider FastAPI endpoint over a single
uploaded image) into a fixed-parameter automated check over a whole
PDF's rendered pages. Kept from the original: the core JPEG-recompress-
and-diff computation (`_compress_jpg`, the non-linear `sqrt(diff)`
error term — the reference tool's default; its `linear=True` branch is
unused here). Dropped: the FastAPI/form scaffolding, and the LUT
contrast-stretch + desaturate steps the reference tool used only to
produce a nicer-to-*look-at* preview image — not needed here since we
threshold the raw error magnitude ourselves (see _regions_for_page)
rather than render one, and SPECIFICATION.md 4 says not to generate/store any
such image anyway. Added: connected-components region-finding +
normalized bounding boxes, so a flag points at a place on the page, not
just a yes/no.

Known limitation (do not oversell in UI copy per SPECIFICATION.md section 4):
ELA is a probabilistic signal, not a standalone verdict — weak against
print-and-rescan tampering, and prone to false positives on legitimately
recompressed scans or already-JPEG-heavy source images. Every finding
below carries that caveat in its description.

Tested and NOT fully resolved: on a rendered text-heavy page, ordinary
crisp glyph edges produce JPEG-recompression error at least as strong as
a realistic tampered patch — a first threshold pass (Otsu, picking
roughly the page's own median split) flagged 35 regions scattered across
an untampered invoice's normal paragraphs, so it was replaced with the
page-relative-percentile approach below, which eliminated every false
positive tested (all 12 of this repo's real sample documents, plus
several synthetic clean pages) but ALSO failed to flag two different
synthetic tampered patches spliced into real invoice pages in testing —
the tamper signal and ordinary text-edge noise turned out to be too
close in magnitude on this asset type for percentile thresholding alone
to separate. This repo has no genuinely tampered *and* image-bearing
sample to validate against (has_image_content gates this check out
entirely for every current sample — none embed a raster image), and
ELA's classic strength is continuous-tone photo/scan content, not
vector-rendered text, so real sensitivity here is unverified pending a
real scanned/photo-bearing tampered sample. Specificity (not
false-flagging clean documents) is the property this implementation is
actually confirmed to hold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np

from app.services.forensics.pdf_render import RenderedPage

Severity = Literal["info", "low", "medium", "high"]

# --- Fixed parameters (SPECIFICATION.md: an automated check, not the reference
# tool's interactive one — nothing here is user-tunable) ---

# The reference tool's own default JPEG requality/output-scale.
JPEG_RECOMPRESS_QUALITY = 75
_SCALE = 50
_ERROR_GAIN = _SCALE / 20  # the reference tool's `scale / 20` term

# A connected component smaller than this fraction of the page area is
# treated as recompression-grid noise, not a genuine localized anomaly.
MIN_REGION_AREA_FRACTION = 0.002
# A component covering more of the page than this reads as uniform
# whole-page recompression noise — legitimately recompressed scans do
# this everywhere (SPECIFICATION.md's named false-positive risk) — not a
# localized edit, so it's excluded rather than flagged.
MAX_REGION_AREA_FRACTION = 0.6

# NOT Otsu (see _regions_for_page's earlier approach, replaced after
# testing). Otsu picks roughly a 50/50 high/low split of THIS page's own
# error-map histogram — fine for a photo with one edited patch standing
# out against mostly-flat background, but a rendered text page's every
# glyph edge already reads as "high error" under JPEG recompression, so
# Otsu's split lands inside ordinary text noise: a real test run flagged
# 35 scattered regions across an untampered page's normal paragraphs
# (p99 of that page's own error distribution: 149/255 — *higher* than
# the mean error measured inside a synthetic tampered patch on a
# different page, 27/255). A fixed high percentile of the page's own
# distribution is far more conservative: only the page's own most
# extreme ~0.3% of pixels count as "high", so ordinary text-edge noise
# (however elevated it runs page-wide) has to be beaten by something
# genuinely more anomalous than the page's own baseline before it's a
# region at all.
_ERROR_PERCENTILE_THRESHOLD = 99.7
# Applied to `_ERROR_PERCENTILE_THRESHOLD`'s outcome so an
# almost-uniformly-flat page (a mostly-blank page with one small image)
# doesn't have its top-0.3%-of-pixels treated as "high error" when
# every pixel on it is already near zero.
_MIN_ABSOLUTE_ERROR_THRESHOLD = 60

# copymove.py's own thresholds for its two anti-forensics signals.
_ANTI_FORENSIC_NOISE_VARIANCE_THRESHOLD = 5.0
_ANTI_FORENSIC_LAPLACIAN_STDDEV_THRESHOLD = 1.5


@dataclass
class Finding:
    finding: str
    severity: Severity
    description: str
    page: int | None = None
    bounding_box: dict[str, Any] | None = None
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "finding": self.finding,
            "severity": self.severity,
            "description": self.description,
        }
        if self.page is not None:
            result["page"] = self.page
        if self.bounding_box is not None:
            result["bounding_box"] = self.bounding_box
        if self.data is not None:
            result["data"] = self.data
        return result


def _compress_jpg(image: np.ndarray, quality: int) -> np.ndarray:
    """Round-trips `image` through JPEG at `quality` — the recompression
    step ELA measures the error against. From the reference tool's
    `compress_jpg`, unchanged."""
    _, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def _error_map(image: np.ndarray) -> np.ndarray:
    """Per-pixel recompression error as an 8-bit grayscale map — the
    reference tool's non-linear ELA path (its default), minus the LUT
    contrast-stretch/desaturate display step (see module docstring)."""
    original = image.astype(np.float32) / 255
    compressed = _compress_jpg(image, JPEG_RECOMPRESS_QUALITY).astype(np.float32) / 255
    difference = cv2.absdiff(original, compressed)
    error = cv2.convertScaleAbs(cv2.sqrt(difference) * 255, None, _ERROR_GAIN)
    return cv2.cvtColor(error, cv2.COLOR_BGR2GRAY)


# Reused directly from the reference copy-move tool's
# `detect_anti_forensics` — fully self-contained (cv2/numpy only), just
# renamed to a private module function. Used here as a *supporting*
# signal on the ELA check (not its own check_type, per spec): if a page
# was blurred or over-compressed to erase ELA's own evidence, that
# attempt is itself worth flagging — and more strongly than an ordinary
# recompression-error region, since it suggests deliberate cover-up
# rather than an incidental artifact.
def _detect_anti_forensics(gray: np.ndarray) -> list[str]:
    warnings: list[str] = []

    blurred = cv2.medianBlur(gray, 3)
    diff = gray.astype(np.float32) - blurred.astype(np.float32)
    noise_variance = float(np.var(diff))
    if noise_variance < _ANTI_FORENSIC_NOISE_VARIANCE_THRESHOLD:
        warnings.append(f"possible anti-forensic smoothing detected (noise variance={noise_variance:.2f})")

    gray_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    laplacian = cv2.Laplacian(gray_bgr, cv2.CV_64F)
    _, stddev = cv2.meanStdDev(laplacian)
    stddev_value = float(stddev[0][0])
    if stddev_value < _ANTI_FORENSIC_LAPLACIAN_STDDEV_THRESHOLD:
        warnings.append(f"possible over-compression detected (Laplacian stddev={stddev_value:.2f})")

    return warnings


def _regions_for_page(page: RenderedPage) -> list[Finding]:
    page_area = page.width * page.height
    error_gray = _error_map(page.image)
    threshold_value = max(
        float(np.percentile(error_gray, _ERROR_PERCENTILE_THRESHOLD)),
        _MIN_ABSOLUTE_ERROR_THRESHOLD,
    )
    _, binary = cv2.threshold(error_gray, threshold_value, 255, cv2.THRESH_BINARY)
    # High-error pixels from a genuinely anomalous region aren't always
    # 8-connected to each other — spliced-in photographic noise, in
    # particular, thresholds into scattered near-isolated pixels rather
    # than one solid blob (uncorrelated neighboring noise values), so
    # each fragment fell under MIN_REGION_AREA_FRACTION on its own even
    # though the whole patch was obviously anomalous (confirmed in
    # testing: a spliced-in noise patch was invisible until this closing
    # step was added). A small morphological close bridges those gaps
    # into one component without also merging genuinely separate regions
    # elsewhere on the page.
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)

    findings: list[Finding] = []
    for label in range(1, num_labels):  # label 0 is the background component
        x, y, w, h, area = stats[label]
        area_fraction = area / page_area
        if area_fraction < MIN_REGION_AREA_FRACTION or area_fraction > MAX_REGION_AREA_FRACTION:
            continue
        findings.append(
            Finding(
                finding="recompression_error_region",
                severity="medium",
                description=(
                    f"Page {page.page_number}: a localized area shows a higher JPEG "
                    "recompression error than the rest of the page — a possible sign "
                    "this area was edited after the page was last saved. This is a "
                    "probabilistic signal, not a definitive finding; ordinary scan/"
                    "print artifacts can also trigger it."
                ),
                page=page.page_number,
                bounding_box={
                    "page": page.page_number,
                    "x": round(x / page.width, 4),
                    "y": round(y / page.height, 4),
                    "width": round(w / page.width, 4),
                    "height": round(h / page.height, 4),
                },
                data={"region_area_fraction": round(area_fraction, 4)},
            )
        )

    gray = cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY)
    for warning in _detect_anti_forensics(gray):
        findings.append(
            Finding(
                finding="anti_forensic_signal",
                # Elevated above a plain recompression-error region — an
                # apparent attempt to erase ELA's own evidence is a
                # stronger signal than the evidence itself.
                severity="high",
                description=f"Page {page.page_number}: {warning} — could indicate an attempt to hide edit traces.",
                page=page.page_number,
            )
        )

    return findings


def run_ela_check(pages: list[RenderedPage]) -> dict[str, Any]:
    """`pages` — the shared render from app/services/forensics/
    pdf_render.py (app/tasks/tampering_checks_task.py renders once and
    passes the same list into both this and copy_move.run_copy_move_check).

    Returns {"result": "pass"|"flag"|"not_applicable", "details": [...]}
    ready to store directly in a document_checks.result jsonb column —
    `details` is a list of Finding dicts, the same shape app/services/
    forensics/metadata_forensics.py already uses, so the existing
    findings-list UI renders it with no frontend changes; `page` and
    `bounding_box` are the two extra fields the highlight-overlay reads.
    """
    analyzable_pages = [p for p in pages if p.has_image_content]
    if not analyzable_pages:
        # Born-digital, text-native document — no photographic/scanned
        # content anywhere for a recompression-error signal to exist in.
        return {
            "result": "not_applicable",
            "details": [{
                "finding": "ela_scope",
                "severity": "info",
                "description": "Not applicable: born-digital, no images — there is no photographic or scanned "
                "content for error level analysis to examine.",
                "data": {"reason": "born-digital, no images"},
            }],
        }
    findings: list[Finding] = []
    for page in analyzable_pages:
        findings.extend(_regions_for_page(page))

    return {"result": "flag" if findings else "pass", "details": [f.to_dict() for f in findings]}
