"""
Visual inconsistency review (SPECIFICATION.md section 3.2's "Image/PDF
manipulation analysis" secondary vision-model pass), folded together
with section 3.4's AI-generated-document detection into ONE check
rather than two — see app/services/ai_content_detection.py's docstring
for the full reasoning: Azure AI Content Safety (the API SPECIFICATION.md
section 2 names for AI-generated-content detection) turns out to have
no synthetic-content/AI-generated-image detection capability at all
(confirmed against Microsoft's own "what's new" changelog and REST API
reference as of 2026-09 — its only image endpoint scores four harm
categories: Hate, SelfHarm, Sexual, Violence), and Hive Moderation
(SPECIFICATION.md's named alternative) has no workable free tier for this
project. Rather than call a nonexistent endpoint or stand up a paid
third-party account for a local dev foundation, the AI-generation
question is asked as a sixth structured item alongside the five visual-
consistency categories in the SAME per-page vision-model call — one
check/one vendor, not two.

Uses the existing Azure OpenAI deployment (app/services/llm_service.py),
the same model behind classification and extraction.

Runs on EVERY uploaded PDF page, unconditionally — NOT gated on
ela_tampering/copy_move_detection firing first (SPECIFICATION.md's ask: a
different technique, model-based visual judgment rather than
pixel-level analysis, that can catch what those miss, or agree with
them and strengthen confidence).

Reliability: unlike ELA/copy-move (deterministic pixel math), a
vision-model judgment can vary run-to-run. Each page is sent to the
model TWICE, independently (app/services/llm_service.py's
analyze_page_visual_consistency, called twice per page at a
deliberately non-zero temperature — see that method's own comment for
why). A finding only survives into `details` as a real signal if it
was reported in BOTH runs, or reported with "high" confidence in at
least one run; a single low/medium-confidence one-off report is
treated as noise and dropped. Every surviving finding's `data` keeps
both runs' raw model output so a reviewer/developer can see exactly
what was said and why it did or didn't survive.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np

from app.services.forensics.pdf_render import RenderedPage
from app.services.llm_service import LLMService, PageVisualAnalysis, VisualCategoryFinding

Severity = Literal["info", "low", "medium", "high"]

# Fixed parameters (this is an automated check, not a tunable one —
# same posture as app/services/forensics/ela.py's fixed parameters).
RUNS_PER_PAGE = 2
# gpt-4o/gpt-4.1-family vision inputs are downscaled well past this on
# Azure's side anyway (documented ~2000px-long-edge tiling behavior) —
# sending more only spends upload time/tokens for no extraction gain.
MAX_IMAGE_DIMENSION = 1536

# Maps each PageVisualAnalysis attribute name to its human-readable
# label — iterated in app/tasks/visual_inconsistency_task.py-invoked
# run_visual_inconsistency_review below. ai_generation_assessment is
# handled separately (_ai_generation_finding) since its shape differs
# (likely_ai_generated, not consistent/inconsistent).
_CATEGORY_LABELS: dict[str, str] = {
    "font_consistency": "Font consistency",
    "text_alignment": "Text alignment",
    "color_contrast_consistency": "Color / contrast consistency",
    "resolution_sharpness_consistency": "Resolution / sharpness consistency",
    "shadow_lighting_consistency": "Shadow / lighting consistency",
}

_CONFIDENCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Finding:
    """Same shape as app/services/forensics/ela.py's Finding — so
    `details` renders in the frontend's existing generic findings-list
    UI (frontend/src/components/case/DocumentChecksPanel.tsx) with no
    special-casing. Also reused by app/tasks/visual_inconsistency_task.py
    for the metadata_forensics cross-check note."""

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


def _encode_page_image(page: RenderedPage) -> str:
    """BGR page render -> base64 PNG data: URI for the vision call. PNG
    (not JPEG) so glyph edges stay crisp for the font/alignment
    categories — JPEG recompression artifacts are exactly the kind of
    thing this check is trying to read past, not introduce."""
    image = page.image
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > MAX_IMAGE_DIMENSION:
        scale = MAX_IMAGE_DIMENSION / longest
        image = cv2.resize(
            image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
        )
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("Failed to encode page image for visual inconsistency review")
    return f"data:image/png;base64,{base64.b64encode(buffer).decode('ascii')}"


def _category_finding(
    category: str,
    label: str,
    page: RenderedPage,
    run1: VisualCategoryFinding,
    run2: VisualCategoryFinding,
) -> Finding:
    """Always returns a Finding — one of THREE shapes, not just a flag/
    no-flag binary, so a reviewer sees what every category actually
    concluded rather than only hearing about the ones that tripped:
      1. Both runs said consistent -> a plain "no issue" info Finding.
      2. One run reported something but it wasn't corroborated (not
         2-of-2, not high-confidence) -> still surfaced, but explicitly
         labeled not-confirmed/noise rather than silently dropped.
      3. A real, corroborated finding (2-of-2 agreement, or high
         confidence in at least one run) -> the flagged Finding, as
         before."""
    reports = [r for r in (run1, run2) if not r.consistent]
    if not reports:
        return Finding(
            finding=f"visual_{category}",
            severity="info",
            description=f"Page {page.page_number} — {label}: no inconsistency detected (both runs agreed).",
            page=page.page_number,
            data={
                "category": category,
                "run_agreement": "consistent in both runs",
                "run_1": run1.model_dump(),
                "run_2": run2.model_dump(),
            },
        )

    agreed_both_runs = not run1.consistent and not run2.consistent
    high_confidence_once = any(r.confidence == "high" for r in reports)
    if not (agreed_both_runs or high_confidence_once):
        # A single low/medium-confidence report with no corroboration —
        # not promoted to a real finding (module docstring), but still
        # shown so a reviewer can see the model wasn't entirely silent
        # on this category, just not confident enough to act on it.
        one_off = reports[0]
        return Finding(
            finding=f"visual_{category}",
            severity="info",
            description=(
                f"Page {page.page_number} — {label}: one run reported a possible issue "
                f"({one_off.description}) but it wasn't corroborated by the second run or "
                "reported with high confidence — treated as noise, not a confirmed finding."
            ),
            page=page.page_number,
            data={
                "category": category,
                "run_agreement": "1-of-2 runs, not corroborated",
                "run_1": run1.model_dump(),
                "run_2": run2.model_dump(),
            },
        )

    best = max(reports, key=lambda r: _CONFIDENCE_RANK.get(r.confidence or "low", 0))
    severity: Severity = (
        "high" if agreed_both_runs and high_confidence_once
        else "medium" if agreed_both_runs or high_confidence_once
        else "low"
    )
    run_agreement = "2-of-2 runs" if agreed_both_runs else "1-of-2 runs (high confidence)"

    bounding_box: dict[str, Any] | None = None
    for r in reports:
        if r.bounding_box is not None:
            bounding_box = {
                "page": page.page_number,
                "x": round(r.bounding_box.x, 4),
                "y": round(r.bounding_box.y, 4),
                "width": round(r.bounding_box.width, 4),
                "height": round(r.bounding_box.height, 4),
            }
            break

    return Finding(
        finding=f"visual_{category}",
        severity=severity,
        description=(
            f"Page {page.page_number} — {label}: {best.description or 'inconsistency reported'} "
            "This is a vision-model judgment, not pixel-level analysis — a probabilistic "
            "signal, not a definitive finding."
        ),
        page=page.page_number,
        bounding_box=bounding_box,
        data={
            "category": category,
            "run_agreement": run_agreement,
            "run_1": run1.model_dump(),
            "run_2": run2.model_dump(),
        },
    )


def _ai_generation_finding(page: RenderedPage, run1: Any, run2: Any) -> Finding:
    flagged_reports = [r for r in (run1, run2) if r.likely_ai_generated]
    agreed_both_runs = run1.likely_ai_generated and run2.likely_ai_generated
    high_confidence_once = any(r.confidence == "high" for r in flagged_reports)
    flagged = bool(flagged_reports) and (agreed_both_runs or high_confidence_once)

    if flagged:
        severity: Severity = (
            "high" if agreed_both_runs and high_confidence_once
            else "medium" if agreed_both_runs or high_confidence_once
            else "low"
        )
        best = max(flagged_reports, key=lambda r: _CONFIDENCE_RANK.get(r.confidence, 0))
        run_agreement = "2-of-2 runs" if agreed_both_runs else "1-of-2 runs (high confidence)"
        description = (
            f"Page {page.page_number}: the model reports possible signs of AI-generated/"
            f"synthetic content — {best.description} This is an experimental, probabilistic "
            "signal only; real-world accuracy on scanned business documents (as opposed to the "
            "photos/art these detectors are usually tuned for) is unproven."
        )
    else:
        severity = "info"
        run_agreement = "not flagged"
        description = f"Page {page.page_number}: no notable signs of AI-generated/synthetic content reported."

    return Finding(
        finding="ai_generation_assessment",
        severity=severity,
        description=description,
        page=page.page_number,
        data={
            "flagged": flagged,
            "run_agreement": run_agreement,
            "run_1": run1.model_dump(),
            "run_2": run2.model_dump(),
        },
    )


# ---------------------------------------------------------------------------
# Post-filters: things the vision model reports that are not anomalies
# ---------------------------------------------------------------------------
#
# Two kinds of model findings are explained by the page itself, and are kept
# in `details` for the reviewer but demoted to "info" (not scored), with the
# reason, by `apply_region_filters`:
#
#  1. Alignment and sharpness findings ABOUT A SIGNATURE OR STAMP. Pen
#     strokes and rubber-stamp ink never sit on the typed baseline and are
#     always softer than printed text. A finding is about one when it covers
#     at least _REGION_OVERLAP of a detected signature/stamp region
#     (signature_stamp_detection) and either names a signature/stamp in its
#     text or is itself mostly that region. The model's boxes are rough
#     (observed 0.03-0.1 of the page off, see signature_detection_service),
#     so its box is grown by _MODEL_BOX_MARGIN before measuring.
#
#  2. Tilt/rotation findings that are just the scan's skew. The text
#     angle inside the finding's box is measured (projection profile) and
#     compared with the page's overall skew and with the bands of page just
#     above and below it; within _SKEW_TOLERANCE_DEG of any of them it is the
#     page's own skew (or the gradual warp of a photographed page that was
#     printed and scanned again), not a pasted-in element, which would
#     differ from its surroundings.
#
# Filtering is reversible and idempotent: a demoted finding keeps its
# original severity and description in data["region_filter"], and every call
# first restores them, so it can be re-run when the signature detection
# arrives later (the two checks run in parallel).

_FILTERED_CATEGORIES = {"visual_text_alignment", "visual_resolution_sharpness_consistency"}
_REGION_OVERLAP = 0.30
_MODEL_BOX_MARGIN = 0.08
_SIGNATURE_WORDS_RE = re.compile(r"signature|signed|stamp|seal|handwrit|initials", re.IGNORECASE)
# Only findings about ROTATION are judged by angle: a row shifted up/down or
# sideways ("misaligned", "offset", "not on the baseline") is a position
# claim that matching angles says nothing about, so those are never dropped
# by the skew test.
_TILT_WORDS_RE = re.compile(r"tilt|rotat|skew|slant|angled|crooked|askew", re.IGNORECASE)
_SKEW_TOLERANCE_DEG = 0.7
_SKEW_SEARCH_DEG = 3.0
_SKEW_STEP_DEG = 0.05
_NEIGHBOUR_BAND = 0.08  # page height above/below a region compared with it
_MIN_INK_PIXELS = 200
# Findings that are notes about the scored ones, re-derived after filtering.
_NOTE_FINDINGS = {"experimental_signal_notice", "metadata_forensics_correlation"}


def _area(box: dict[str, float]) -> float:
    return max(0.0, box["width"]) * max(0.0, box["height"])


def _intersection(a: dict[str, float], b: dict[str, float]) -> float:
    w = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    return max(0.0, w) * max(0.0, h)


def _grown(box: dict[str, float], margin: float) -> dict[str, float]:
    x, y = max(0.0, box["x"] - margin), max(0.0, box["y"] - margin)
    return {
        "x": x,
        "y": y,
        "width": min(1.0, box["x"] + box["width"] + margin) - x,
        "height": min(1.0, box["y"] + box["height"] + margin) - y,
    }


def _signature_regions(signature_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    details = (signature_result or {}).get("details")
    detected = details.get("detected") if isinstance(details, dict) else None
    regions = []
    for item in detected or []:
        box = item.get("bounding_box") if isinstance(item, dict) else None
        if isinstance(box, dict) and all(isinstance(box.get(k), (int, float)) for k in ("x", "y", "width", "height")):
            regions.append({"kind": item.get("kind") or "signature", **box})
    return regions


def _signature_overlap(finding: dict[str, Any], regions: list[dict[str, Any]]) -> str | None:
    box = finding.get("bounding_box")
    if not isinstance(box, dict) or not regions:
        return None
    grown = _grown(box, _MODEL_BOX_MARGIN)
    names_one = bool(_SIGNATURE_WORDS_RE.search(finding.get("description") or ""))
    for region in regions:
        if region.get("page", box.get("page")) != box.get("page") or _area(region) <= 0:
            continue
        inter = _intersection(grown, region)
        if inter / _area(region) >= _REGION_OVERLAP and (names_one or inter / max(_area(grown), 1e-9) >= _REGION_OVERLAP):
            why = (
                "handwritten signatures and stamps never follow the typed text's baseline"
                if finding.get("finding") == "visual_text_alignment"
                else "pen strokes and rubber-stamp ink are naturally softer than printed text"
            )
            return f"it is about the detected {region['kind']} (covers {inter / _area(region):.0%} of it) — {why}"
    return None


def _ink(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def estimate_skew(ink: np.ndarray, *, scale: float = 1.0) -> float | None:
    """Dominant angle (degrees) of the text lines / rules in a binarized
    region: the rotation that makes its horizontal projection most peaked
    (projection-profile deskew), searched over ±_SKEW_SEARCH_DEG. None when
    there is too little ink to tell."""
    if scale != 1.0:
        ink = cv2.resize(ink, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ys, xs = np.nonzero(ink)
    if len(xs) < _MIN_INK_PIXELS:
        return None
    xs = xs - ink.shape[1] / 2.0
    ys = ys.astype(float)
    best, best_score = None, -1.0
    for deg in np.arange(-_SKEW_SEARCH_DEG, _SKEW_SEARCH_DEG + 1e-9, _SKEW_STEP_DEG):
        projected = np.round(ys - xs * np.tan(np.deg2rad(deg)))
        hist = np.bincount((projected - projected.min()).astype(int))
        score = float(np.dot(hist, hist))
        if score > best_score:
            best, best_score = float(deg), score
    return None if best is None else round(best, 2)


def _crop(ink: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    h, w = ink.shape[:2]
    return ink[int(max(0.0, y0) * h):int(min(1.0, y1) * h), int(max(0.0, x0) * w):int(min(1.0, x1) * w)]


def _page_skew(finding: dict[str, Any], pages_by_number: dict[int, RenderedPage], cache: dict) -> str | None:
    box = finding.get("bounding_box")
    page = pages_by_number.get((box or {}).get("page") or finding.get("page"))
    if not isinstance(box, dict) or page is None or not _TILT_WORDS_RE.search(finding.get("description") or ""):
        return None
    if page.page_number not in cache:
        ink = _ink(page.image)
        cache[page.page_number] = (ink, estimate_skew(_crop(ink, 0.03, 0.03, 0.97, 0.97), scale=0.5))
    ink, page_angle = cache[page.page_number]
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    local = estimate_skew(_crop(ink, x, y, x + w, y + h))
    if local is None:
        return None
    references = {
        "the page's overall skew": page_angle,
        "the page just above it": estimate_skew(_crop(ink, 0.05, y - _NEIGHBOUR_BAND, 0.95, y)),
        "the page just below it": estimate_skew(_crop(ink, 0.05, y + h, 0.95, y + h + _NEIGHBOUR_BAND)),
    }
    for label, angle in references.items():
        if angle is not None and abs(local - angle) <= _SKEW_TOLERANCE_DEG:
            return (
                f"the region's text runs at {local:+.2f}°, within {_SKEW_TOLERANCE_DEG}° of {label} "
                f"({angle:+.2f}°) — skew from scanning, not a separately placed element"
            )
    return None


def apply_region_filters(
    result: dict[str, Any], signature_result: dict[str, Any] | None, pages: list[RenderedPage]
) -> dict[str, Any]:
    """Demote alignment/sharpness findings explained by a signature/stamp or
    by the scan's skew (see the section comment above), then recompute the
    check's result. Returns `result`, changed in place."""
    details = result.get("details")
    if not isinstance(details, list):
        return result
    regions = _signature_regions(signature_result)
    pages_by_number = {p.page_number: p for p in pages}
    cache: dict = {}
    for finding in details:
        if not isinstance(finding, dict) or finding.get("finding") not in _FILTERED_CATEGORIES:
            continue
        data = finding.setdefault("data", {})
        previous = data.pop("region_filter", None)
        if previous:  # restore, then judge again with the current inputs
            finding["severity"] = previous["original_severity"]
            finding["description"] = previous["original_description"]
        if finding.get("severity") not in ("medium", "high"):
            continue
        reason = _signature_overlap(finding, regions)
        if reason is None and finding["finding"] == "visual_text_alignment":
            reason = _page_skew(finding, pages_by_number, cache)
        if reason is None:
            continue
        data["region_filter"] = {
            "original_severity": finding["severity"],
            "original_description": finding["description"],
            "reason": reason,
        }
        finding["severity"] = "info"
        finding["description"] = f"{finding['description']} Not counted: {reason}."

    flagged = any(
        isinstance(f, dict) and f.get("finding") not in _NOTE_FINDINGS and f.get("severity") in ("medium", "high")
        for f in details
    )
    if result.get("result") in ("pass", "flag"):
        result["result"] = "flag" if flagged else "pass"
    if not flagged:
        # A note correlating THIS check's flag with metadata forensics no
        # longer applies once nothing here is flagged.
        result["details"] = [f for f in details if not (isinstance(f, dict) and f.get("finding") == "metadata_forensics_correlation")]
    return result


def run_visual_inconsistency_review(llm: LLMService, pages: list[RenderedPage]) -> dict[str, Any]:
    """Runs the full per-page, double-sampled vision review over every
    rendered page. Returns {"result": "pass"|"flag", "details": [...]}
    ready to store directly in a document_checks.result jsonb column —
    same shape app/services/forensics/ela.py uses. Pure function over an
    already-constructed LLMService + already-rendered pages: no DB
    access here (the metadata_forensics cross-check, which needs a DB
    session, is layered on afterward by app/tasks/visual_inconsistency_
    task.py)."""
    findings: list[Finding] = [
        Finding(
            finding="experimental_signal_notice",
            severity="info",
            description=(
                "This check uses a vision-capable language model's visual judgment, run twice "
                "independently per page — not deterministic pixel-level analysis like error "
                "level analysis or copy-move detection. Every category below is reported "
                "regardless of outcome, including a clean 'no issue' result — a finding only "
                "counts as confirmed (and affects this check's pass/flag result) if it appeared "
                "in both runs or was reported with high confidence in at least one; anything "
                "else is shown but marked as not corroborated. Treat this as one probabilistic, "
                "experimental signal among several, not a standalone verdict."
            ),
        )
    ]

    any_flag = False
    for page in pages:
        image_data_uri = _encode_page_image(page)
        run1 = llm.analyze_page_visual_consistency(image_data_uri)
        run2 = llm.analyze_page_visual_consistency(image_data_uri)

        for category, label in _CATEGORY_LABELS.items():
            finding = _category_finding(category, label, page, getattr(run1, category), getattr(run2, category))
            findings.append(finding)
            if finding.severity in ("medium", "high"):
                any_flag = True

        ai_finding = _ai_generation_finding(page, run1.ai_generation_assessment, run2.ai_generation_assessment)
        findings.append(ai_finding)
        if ai_finding.severity in ("medium", "high"):
            any_flag = True

    return {"result": "flag" if any_flag else "pass", "details": [f.to_dict() for f in findings]}
