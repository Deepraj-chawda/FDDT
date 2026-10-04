"""
Visual inconsistency review (CLAUDE.md section 3.2's "Image/PDF
manipulation analysis" secondary Claude-vision pass), folded together
with section 3.4's AI-generated-document detection into ONE check
rather than two — see app/services/ai_content_detection.py's docstring
for the full reasoning: Azure AI Content Safety (the API CLAUDE.md
section 2 names for AI-generated-content detection) turns out to have
no synthetic-content/AI-generated-image detection capability at all
(confirmed against Microsoft's own "what's new" changelog and REST API
reference as of 2026-09 — its only image endpoint scores four harm
categories: Hate, SelfHarm, Sexual, Violence), and Hive Moderation
(CLAUDE.md's named alternative) has no workable free tier for this
project. Rather than call a nonexistent endpoint or stand up a paid
third-party account for a local dev foundation, the AI-generation
question is asked as a sixth structured item alongside the five visual-
consistency categories in the SAME per-page vision-model call — one
check/one vendor, not two.

Uses the existing Azure OpenAI deployment (app/services/llm_service.py)
— NOT the Claude API, despite CLAUDE.md section 2 naming Claude for
this kind of call; see llm_service.py's own module docstring for why
this project already made that substitution project-wide.

Runs on EVERY uploaded PDF page, unconditionally — NOT gated on
ela_tampering/copy_move_detection firing first (CLAUDE.md's ask: a
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
from dataclasses import dataclass
from typing import Any, Literal

import cv2

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
