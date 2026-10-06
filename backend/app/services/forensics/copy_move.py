"""
Copy-move forgery detection (SPECIFICATION.md section 3.2 "Tampering/
alteration detection"). Looks for a region of a page duplicated
elsewhere on the *same* page — a common way to cover up or fabricate
content (e.g. pasting a clean signature/stamp over an edited line item).

Adapted from an internal reference tool (`copymove.py` — an interactive
detector-type/threshold-slider FastAPI endpoint) into a fixed-parameter
automated check over a whole PDF's rendered pages. Kept from the
reference tool: its keypoint-detect → response-filter → radius-match →
distance/angle clustering pipeline, unchanged. Dropped: the FastAPI/
upload/mask/visualization (colored-line-overlay image) scaffolding —
SPECIFICATION.md 4 says not to generate/store any such image; the frontend
draws the highlight live from the bounding boxes below instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import compress
from typing import Any, Literal

import cv2
import numpy as np

from app.services.forensics.pdf_render import RenderedPage

Severity = Literal["info", "low", "medium", "high"]

# --- Fixed parameters — the reference tool's own function-level
# defaults (as opposed to its FastAPI form's required-with-no-default
# fields), not user-tunable here. ---
DETECTOR_TYPE = "BRISK"
RESPONSE_THRESHOLD = 90
MATCHING_THRESHOLD = 20
DISTANCE_THRESHOLD = 15
# NOT the reference tool's own default (5) — raised after testing found
# 5 flagged all 12 real (untampered) sample documents in this repo, 2 to
# 219 "clusters" each. Business documents are full of *legitimate*
# repeated small shapes (digits, repeated table cells/labels, ruled
# lines) that a low match-count threshold reads as copy-move. Swept
# CLUSTER_SIZE x MIN_REGION_AREA_FRACTION against every sample below and
# picked the first point with zero false positives and a safety margin
# (30/0.02 was the first zero-false-positive point in the sweep; 40+
# was also zero but a stricter floor than the data required). No
# genuinely tampered sample was available in this repo to validate
# sensitivity against, only specificity — revisit this constant once one
# exists.
CLUSTER_SIZE = 30
# A cluster below this fraction of the page's area is a small,
# plausibly-incidental match (a repeated digit/word) even at
# CLUSTER_SIZE's floor — see the constant above for how both were tuned
# together.
MIN_REGION_AREA_FRACTION = 0.02

# NOT part of the reference tool (which used cv2.BRISK_create()'s
# implicit default, thresh=30) — measured against this project's actual
# rendered pages (200 DPI, see pdf_render.py) rather than the reference
# tool's presumably photo-sized test images. A real text-heavy business
# document page renders tens of thousands of small, near-identical
# corner-like keypoints along glyph edges at the default threshold
# (27,630 on one sample invoice) — BFMatcher's radiusMatch is
# effectively O(keypoints^2), so that hung a worker process outright in
# testing (confirmed: still hadn't returned after 90s on a single page).
# thresh=200 cuts that to the 900-5,000 range across every sample this
# was tested against, keeping match+cluster time under ~2s — and is
# arguably *more* correct for this use case anyway: a handful of
# distinctive strong corners are what should match across a genuine
# copy-paste, not thousands of individually-ambiguous text-glyph corners
# (which would also be a real false-positive source: many repeated
# letters on the same page are not evidence of copy-move forgery).
BRISK_THRESHOLD = 200

# Defense in depth beyond the BRISK_THRESHOLD tuning above — if some
# other page still produces more keypoints or matches than this, skip
# clustering for that one page (quiet, informational finding) rather
# than let the O(match_count^2) greedy clustering loop below run
# unbounded. The reference tool treated this as an outright error;
# here it's a per-page skip instead of failing the whole document.
MAX_FILTERED_KEYPOINTS = 4_000
MAX_MATCHES_TO_CLUSTER = 4_000


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


def _detect_clusters(gray: np.ndarray) -> tuple[list[cv2.KeyPoint], list[list[cv2.DMatch]]]:
    """The reference tool's `process_image` core pipeline — fixed
    parameters replacing its form fields, BRISK_THRESHOLD tuned for this
    project's rendered pages (see that constant's comment for why),
    MAX_MATCHES_TO_CLUSTER added as a second bound before the O(n^2)
    clustering loop below, and one correctness fix (see the match_dists
    comment). Returns (keypoints, clusters) — each cluster is a list of
    matches, each match's .queryIdx/.trainIdx indexing into `keypoints`."""
    detector = cv2.BRISK_create(thresh=BRISK_THRESHOLD)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if not keypoints:
        return [], []

    responses = np.array([k.response for k in keypoints])
    response_thresh = 100 - RESPONSE_THRESHOLD
    strong_mask = (
        cv2.normalize(responses, None, 0, 100, cv2.NORM_MINMAX) >= response_thresh
    ).flatten()
    keypoints = list(compress(keypoints, strong_mask))
    descriptors = descriptors[strong_mask]

    if len(keypoints) > MAX_FILTERED_KEYPOINTS:
        return keypoints, []

    matcher = cv2.BFMatcher_create(cv2.NORM_HAMMING, True)
    match_thresh = MATCHING_THRESHOLD / 100 * 255
    raw_matches = matcher.radiusMatch(descriptors, descriptors, match_thresh)
    matches = [m for sublist in raw_matches for m in sublist if m.queryIdx != m.trainIdx]
    if not matches:
        return keypoints, []

    kpts_pts = np.array([k.pt for k in keypoints])
    min_dist = (DISTANCE_THRESHOLD / 100) * np.min(gray.shape) / 2
    match_dists = np.linalg.norm(
        [kpts_pts[m.queryIdx] - kpts_pts[m.trainIdx] for m in matches], axis=1
    )
    # Matches between two keypoints that are already near each other
    # aren't "moved" content — nothing to flag. Filtered together with
    # their distances (the reference tool filtered `matches` this way
    # but then indexed the *original*, still-full `match_dists` array by
    # the *filtered* list's positions below — a real bug, fixed here by
    # keeping the two aligned through the filter instead of after it).
    kept = [(m, d) for m, d in zip(matches, match_dists) if d > min_dist]
    if not kept:
        return keypoints, []
    matches, match_dists = (list(x) for x in zip(*kept))

    if len(matches) > MAX_MATCHES_TO_CLUSTER:
        return keypoints, []

    clusters: list[list[cv2.DMatch]] = []
    for i, match0 in enumerate(matches):
        group = [match0]
        query0, train0 = match0.queryIdx, match0.trainIdx
        d0 = match_dists[i]

        for j in range(i + 1, len(matches)):
            match1 = matches[j]
            query1, train1 = match1.queryIdx, match1.trainIdx
            if query1 == train0 and train1 == query0:
                continue
            d1 = match_dists[j]
            if abs(d0 - d1) > min_dist:
                continue

            a0 = np.array(keypoints[query0].pt)
            b0 = np.array(keypoints[train0].pt)
            a1 = np.array(keypoints[query1].pt)
            b1 = np.array(keypoints[train1].pt)
            aa, bb = np.linalg.norm(a0 - a1), np.linalg.norm(b0 - b1)
            ab, ba = np.linalg.norm(a0 - b1), np.linalg.norm(b0 - a1)
            if not (
                (0 < aa < min_dist and 0 < bb < min_dist)
                or (0 < ab < min_dist and 0 < ba < min_dist)
            ):
                continue
            if any(g.queryIdx == train1 and g.trainIdx == query1 for g in group):
                continue
            group.append(match1)

        if len(group) >= CLUSTER_SIZE:
            clusters.append(group)

    return keypoints, clusters


# Above this IoU, two clusters are the same underlying duplicated region
# found by different starting matches, not two separate findings — the
# greedy clustering in _detect_clusters commonly produces many
# overlapping groups for one real duplicate area (confirmed in testing:
# a single synthetic copy-paste produced 21 near-identical clusters
# before this dedup). Kept deliberately loose (0.3, not e.g. 0.7) so
# genuinely adjacent-but-distinct regions still survive as separate
# findings.
_DEDUP_IOU_THRESHOLD = 0.3


def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return intersection / (area_a + area_b - intersection)


def run_copy_move_check(pages: list[RenderedPage]) -> dict[str, Any]:
    """`pages` — the shared render from app/services/forensics/
    pdf_render.py (the same list app/tasks/tampering_checks_task.py also
    passes to ela.run_ela_check).

    Returns {"result": "pass"|"flag", "details": [...]} — `details` is a
    list of Finding dicts (see ela.py for why this shape), one per
    *distinct* detected region (see _bbox_iou/_DEDUP_IOU_THRESHOLD —
    heavily overlapping clusters are the same region, deduplicated
    before this returns), each carrying `page` and a `bounding_box`
    around that region's matched keypoints.
    """
    findings: list[Finding] = []

    for page in pages:
        gray = cv2.cvtColor(page.image, cv2.COLOR_BGR2GRAY)
        keypoints, clusters = _detect_clusters(gray)

        candidates: list[tuple[tuple[float, float, float, float], int]] = []
        for cluster in clusters:
            xs: list[float] = []
            ys: list[float] = []
            for match in cluster:
                for pt in (keypoints[match.queryIdx].pt, keypoints[match.trainIdx].pt):
                    xs.append(pt[0])
                    ys.append(pt[1])
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)
            width_frac = (x_max - x_min) / page.width
            height_frac = (y_max - y_min) / page.height
            if width_frac * height_frac < MIN_REGION_AREA_FRACTION:
                continue
            candidates.append(((x_min, y_min, x_max, y_max), len(cluster)))

        # Non-max suppression: strongest cluster (most matched pairs)
        # first, drop anything that heavily overlaps a region already kept.
        candidates.sort(key=lambda c: c[1], reverse=True)
        kept: list[tuple[tuple[float, float, float, float], int]] = []
        for box, matched_pairs in candidates:
            if any(_bbox_iou(box, kept_box) > _DEDUP_IOU_THRESHOLD for kept_box, _ in kept):
                continue
            kept.append((box, matched_pairs))

        for (x_min, y_min, x_max, y_max), matched_pairs in kept:
            findings.append(
                Finding(
                    finding="copy_move_cluster",
                    severity="high",
                    description=(
                        f"Page {page.page_number}: found a cluster of {matched_pairs} matching "
                        "feature pairs, consistent with one region of this page having been "
                        "copied and pasted elsewhere on the same page."
                    ),
                    page=page.page_number,
                    bounding_box={
                        "page": page.page_number,
                        "x": round(x_min / page.width, 4),
                        "y": round(y_min / page.height, 4),
                        "width": round((x_max - x_min) / page.width, 4),
                        "height": round((y_max - y_min) / page.height, 4),
                    },
                    data={"matched_pairs": matched_pairs},
                )
            )

    return {"result": "flag" if findings else "pass", "details": [f.to_dict() for f in findings]}
