"""
Signature comparison service — crop-and-upload and vision-comparison
logic for the signature verification flow (SPECIFICATION.md §2.3).

Two responsibilities:
  1. `crop_and_upload_signature` — given a document's blob URL and a
     normalized bounding box, render the relevant PDF page, crop the
     region, encode as PNG, and upload to Blob Storage. Returns the
     durable URL of the crop.

  2. `encode_signature_for_comparison` — given a blob URL for an
     already-uploaded crop, downloads the bytes and encodes them as a
     base64 data URI ready to pass to the LLM's image_url content part.

Detection (existing `signature_stamp_detection` check) and comparison
(this module) are intentionally separate: detection runs automatically
on every upload; comparison ONLY runs when a reviewer explicitly creates
a reference (app/tasks/signature_comparison_task.py). No automatic
cross-document comparison happens at upload time.
"""
from __future__ import annotations

import base64
import io
import uuid
from typing import TYPE_CHECKING

from app.services.forensics.pdf_render import render_pdf_pages

if TYPE_CHECKING:
    from app.services.storage_service import StorageService


def crop_and_upload_signature(
    storage: "StorageService",
    document_blob_url: str,
    bounding_box: dict,
    reference_id: uuid.UUID,
    *,
    company_id: uuid.UUID,
    case_id: uuid.UUID,
) -> str | None:
    """Download `document_blob_url`, render the page specified by
    `bounding_box['page']`, crop the normalized region, encode as PNG,
    upload to Blob Storage, and return the durable URL.

    Returns None if the PDF can't be rendered (e.g. not a PDF, corrupt
    file) — callers treat a None crop URL as "reference created but
    image unavailable" and proceed without blocking the reference row.

    `bounding_box` format: {page (1-based int), x, y, width, height}
    all in [0, 1] page-fraction units (same convention as every other
    bounding box in this codebase).
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        # cv2/numpy unavailable — can't do pixel crop. Shouldn't happen
        # in production since copy_move.py already depends on both.
        return None

    try:
        pdf_bytes = storage.download_bytes(document_blob_url)
    except Exception:  # noqa: BLE001
        return None

    page_number = int(bounding_box.get("page", 1))
    pages = render_pdf_pages(pdf_bytes)
    if not pages:
        return None

    target_pages = [p for p in pages if p.page_number == page_number]
    if not target_pages:
        target_pages = [pages[0]]

    img = target_pages[0].image
    if img is None:
        return None

    h, w = img.shape[:2]

    x = float(bounding_box.get("x", 0))
    y = float(bounding_box.get("y", 0))
    bw = float(bounding_box.get("width", 0))
    bh = float(bounding_box.get("height", 0))

    # Clamp to image bounds
    x1 = max(0, int(x * w))
    y1 = max(0, int(y * h))
    x2 = min(w, int((x + bw) * w))
    y2 = min(h, int((y + bh) * h))

    if x2 <= x1 or y2 <= y1:
        return None

    crop = img[y1:y2, x1:x2]

    # Encode crop as PNG bytes
    success, png_buffer = cv2.imencode(".png", crop)
    if not success:
        return None
    png_bytes = png_buffer.tobytes()

    from app.services.storage_service import signature_blob_path  # noqa: PLC0415

    blob_path = signature_blob_path(company_id, case_id, reference_id)
    try:
        return storage.upload(blob_path, png_bytes, content_type="image/png")
    except Exception:  # noqa: BLE001
        return None


def encode_image_as_data_uri(image_bytes: bytes, content_type: str = "image/png") -> str:
    """Encode raw image bytes as a base64 data URI for passing to the
    LLM vision content part (`image_url.url`)."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{b64}"


# ---------------------------------------------------------------------------
# Pixel-identical reuse check (classical CV — no model, SPECIFICATION.md §4)
# ---------------------------------------------------------------------------
#
# A vision model asked "do these look alike?" answers "consistent" for two
# identical images — reassurance, when the real signal is the opposite: a
# person never signs pixel-for-pixel the same way twice, so an exact match
# means the same image file was placed in both documents. That is legitimate
# for a stored e-signature image, so alone it is only a modest flag; it is a
# strong one when the printed signer text under the two signatures differs.
#
# Only applied to handwritten signatures, never stamps: a company's rubber
# stamp is legitimately identical every time it appears.

# Two crops of the same source image after resampling correlate at ~0.97+;
# even the closest genuine signings by one hand sit well under 0.8.
REUSE_CORRELATION_THRESHOLD = 0.93
_CANONICAL_WIDTH = 256
_INK_GRAY_MAX = 215
_MIN_INK_PIXELS = 50
_MAX_ASPECT_DIFFERENCE = 0.06  # relative; differently-shaped ink can't be the same image
_SHIFT_TOLERANCE_PX = 4  # residual misalignment absorbed at canonical size

# Where to look for the printed signer text: just below the signature, this
# far (fraction of page height), and this much wider than it (fraction of width).
_LABEL_DEPTH = 0.07
_LABEL_MARGIN_X = 0.05
# token_set_ratio below this = the two printed signer lines name different people.
_LABEL_DIFFERENT_BELOW = 60


def _ink_trimmed_gray(image_bytes: bytes):
    """Decode to grayscale and trim to the ink; None if there's too little ink
    for the comparison to mean anything (e.g. a blank crop)."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    gray = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    mask = gray < _INK_GRAY_MAX
    if int(mask.sum()) < _MIN_INK_PIXELS:
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return gray[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]


def signature_pixel_similarity(image_a: bytes, image_b: bytes) -> float | None:
    """Normalized correlation (about -1..1; 1 = identical) between two signature
    crops after trimming to ink, resampling to a common size and allowing a few
    pixels of misalignment. None if either crop has too little ink; 0.0 if the
    ink shapes' proportions differ too much to be the same image."""
    import cv2  # noqa: PLC0415

    a = _ink_trimmed_gray(image_a)
    b = _ink_trimmed_gray(image_b)
    if a is None or b is None:
        return None
    aspect_a = a.shape[1] / a.shape[0]
    aspect_b = b.shape[1] / b.shape[0]
    if abs(aspect_a - aspect_b) / aspect_a > _MAX_ASPECT_DIFFERENCE:
        return 0.0
    size = (_CANONICAL_WIDTH, max(8, round(_CANONICAL_WIDTH / aspect_a)))
    a = cv2.GaussianBlur(cv2.resize(a, size, interpolation=cv2.INTER_AREA), (3, 3), 0)
    b = cv2.GaussianBlur(cv2.resize(b, size, interpolation=cv2.INTER_AREA), (3, 3), 0)
    pad = _SHIFT_TOLERANCE_PX
    b = cv2.copyMakeBorder(b, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    return float(cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED).max())


def printed_label_below(pdf_bytes: bytes, bounding_box: dict) -> str:
    """The first line of printed text directly beneath a signature, read from
    the PDF's own text layer (usually the signer's name). Empty if the file has
    no text layer there — a scan, for instance."""
    import pymupdf  # noqa: PLC0415

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_number = int(bounding_box.get("page", 1))
        if not 1 <= page_number <= len(doc):
            return ""
        page = doc[page_number - 1]
        width, height = page.rect.width, page.rect.height
        x, y = float(bounding_box["x"]), float(bounding_box["y"])
        w, h = float(bounding_box["width"]), float(bounding_box["height"])
        clip = pymupdf.Rect(
            (x - _LABEL_MARGIN_X) * width,
            (y + h) * height,
            (x + w + _LABEL_MARGIN_X) * width,
            (y + h + _LABEL_DEPTH) * height,
        )
        # word = (x0, y0, x1, y1, text, block, line, word_no); keep words whose
        # vertical centre is below the signature so a line above/overlapping it
        # isn't dragged in, yet a name line hugging the signature still counts.
        words = [wd for wd in page.get_text("words", clip=clip) if (wd[1] + wd[3]) / 2 >= clip.y0]
    if not words:
        return ""
    first = min(words, key=lambda wd: (wd[1], wd[5], wd[6]))
    line = sorted((wd for wd in words if wd[5] == first[5] and wd[6] == first[6]), key=lambda wd: wd[7])
    return " ".join(wd[4] for wd in line).strip()


def printed_labels_differ(label_a: str, label_b: str) -> bool:
    """True only when both lines were read AND they name different people;
    a missing label proves nothing either way."""
    from rapidfuzz import fuzz  # noqa: PLC0415

    if not label_a or not label_b:
        return False
    return fuzz.token_set_ratio(label_a, label_b) < _LABEL_DIFFERENT_BELOW


def find_signature_reuse(
    storage: "StorageService",
    *,
    reference_image_bytes: bytes,
    reference_pdf_bytes: bytes | None,
    reference_box: dict | None,
    target_image_url: str,
    target_pdf_url: str,
    target_box: dict,
) -> tuple[str, str] | None:
    """(verdict, reasoning) if the target crop is a pixel-identical reuse of the
    reference signature, else None. Best-effort: any failure returns None so the
    ordinary vision-model comparison still runs."""
    try:
        target_image = storage.download_bytes(target_image_url)
        similarity = signature_pixel_similarity(reference_image_bytes, target_image)
        if similarity is None or similarity < REUSE_CORRELATION_THRESHOLD:
            return None
    except Exception:  # noqa: BLE001
        return None

    reference_label = target_label = ""
    try:
        if reference_pdf_bytes and reference_box:
            reference_label = printed_label_below(reference_pdf_bytes, reference_box)
        target_label = printed_label_below(storage.download_bytes(target_pdf_url), target_box)
    except Exception:  # noqa: BLE001 - labels are context only
        pass

    core = (
        "The signature here is pixel-for-pixel identical to the reference signature, "
        "allowing only for tiny scaling or alignment differences. A handwritten signature "
        "is not reproduced exactly from one signing to the next, so this indicates the same "
        "image file was inserted into both documents."
    )
    if printed_labels_differ(reference_label, target_label):
        return (
            "reused_different_signer",
            f"{core} The printed text beneath the two signatures differs "
            f"(reference: '{reference_label}'; this document: '{target_label}'), so one "
            "signature image appears under two different named signers. This is a strong "
            "reason for manual review; it is a comparison aid, not proof of anything.",
        )
    if reference_label and target_label:
        context = f" The printed signer text beneath both is similar ('{target_label}')."
    else:
        context = " The printed signer text could not be read from the file, so it was not compared."
    return (
        "identical_reuse",
        f"{core}{context} Reusing a stored e-signature image can be legitimate, so check "
        "how each document was actually signed.",
    )


def download_and_encode_signature(
    storage: "StorageService", signature_image_url: str
) -> str | None:
    """Download an already-uploaded signature crop from Blob Storage and
    encode it as a base64 data URI. Returns None on any download failure
    (callers skip this target document rather than erroring the whole task).
    """
    try:
        image_bytes = storage.download_bytes(signature_image_url)
        return encode_image_as_data_uri(image_bytes)
    except Exception:  # noqa: BLE001
        return None
