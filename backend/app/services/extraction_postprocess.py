"""
Deterministic clean-up of the extraction model's fields, applied with the
line-item reading in app/services/line_item_parsing.py
(`enrich_extracted_fields`) before any check reads them.

  - Form template dates. Controlled forms carry a document-control footer
    ("DOCUMENT F-BON-180 | REV. No. 04 | ISSUE No. 71 | DATE OF ISSUE
    14.03.2022"): the date the blank FORM was issued, years before the
    document filled in on it. `label_form_template_dates` finds a "Date of
    issue" that sits beside such revision/issue/form-number markers and labels
    it `form_template_date` (with an ISO `value` and its `raw_text`), relabelling
    the model's own field for it or adding one. A "Date of issue" without those
    markers — a certificate's, a passport's — is the document's own date and
    is left alone.

  - Signature ink read as a name. OCR reads a handwritten signature as a word
    ("Prepared by: Princess" + the scribble below it read as "Dalab"), and the
    model folds it into the person's name. `strip_signature_text` removes,
    from person-role fields (prepared/signed/approved by, principal, ...),
    words that OCR marked handwritten and set much larger than the page's
    text, or that lie inside a detected signature region. The original value
    is kept in `value_as_read`.
"""
from __future__ import annotations

import re
import statistics
from datetime import date
from typing import Any

# A "date of issue" and the date after it ("DATE OF ISSUE -\n14.03.2022").
_DATE_OF_ISSUE_RE = re.compile(
    r"\bDATE\s+OF\s+ISSUE\b[\s:.\-–]*(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{2,4})\b", re.IGNORECASE
)
# Document-control markers around it.
_CONTROL_MARKERS = (
    re.compile(r"\bREV(?:ISION)?\b\.?\s*(?:NO|#)?", re.IGNORECASE),
    re.compile(r"\bISSUE\s*NO\b", re.IGNORECASE),
    re.compile(r"\b(?:DOC(?:UMENT)?|FORM)\s*(?:NO\b|#|[A-Z]{1,4}\s*-)", re.IGNORECASE),
)
# How far around the date its markers may be, in characters of OCR text.
_CONTROL_WINDOW = 200
_MIN_CONTROL_MARKERS = 2

# Additional fields that name the person who prepared/signed the document.
_PERSON_ROLE_RE = re.compile(
    r"prepared|signed|signator|signature|authori[sz]ed|approved|issued_by|received_by|checked|verified|"
    r"cashier|accountant|principal|manager|director|officer|registrar",
    re.IGNORECASE,
)
# A handwritten word this many times the page's median word height is
# signature ink, not handwriting filled into a form.
_SIGNATURE_HEIGHT_RATIO = 1.8


def _template_dates(ocr_text: str) -> list[tuple[str, str]]:
    """(ISO date, raw text) of every "date of issue" inside a document-control block."""
    found = []
    for match in _DATE_OF_ISSUE_RE.finditer(ocr_text or ""):
        window = ocr_text[max(0, match.start() - _CONTROL_WINDOW) : match.end() + _CONTROL_WINDOW]
        if sum(1 for marker in _CONTROL_MARKERS if marker.search(window)) < _MIN_CONTROL_MARKERS:
            continue
        day, month, year = (int(g) for g in match.groups())
        year += 2000 if year < 100 else 0
        try:
            iso = date(year, month, day).isoformat()
        except ValueError:
            continue
        raw = re.search(r"\d{1,2}\s*[./\-]\s*\d{1,2}\s*[./\-]\s*\d{2,4}", match.group(0)).group(0)
        found.append((iso, raw))
    return found


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def label_form_template_dates(fields: dict[str, Any], ocr_text: str | None) -> None:
    """Label a document-control "date of issue" as `form_template_date`
    (module docstring). Mutates `fields`."""
    additional = fields.setdefault("additional_fields", [])
    for iso, raw in _template_dates(ocr_text or ""):
        note = "Date the printed form template was issued (document-control footer), not the document's own date."
        match = next(
            (
                f for f in additional
                if isinstance(f, dict) and (f.get("value") == iso or _digits(f.get("value")) == _digits(raw))
            ),
            None,
        )
        if match is not None:
            if match.get("field_name") != "form_template_date":
                match["field_name_as_extracted"] = match.get("field_name")
            match.update(field_name="form_template_date", value=iso, raw_text=raw, note=note)
        else:
            additional.append(
                {
                    "field_name": "form_template_date", "value": iso, "raw_text": raw, "note": note,
                    "confidence": 1.0, "uncertain": False,
                }
            )


def _box(item: Any) -> tuple[int, float, float, float, float] | None:
    box = item.get("bounding_box") if isinstance(item, dict) else None
    if not isinstance(box, dict):
        return None
    try:
        return int(box.get("page") or 1), box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]
    except (KeyError, TypeError):
        return None


def _overlap(a: tuple, b: tuple) -> float:
    """Share of `a`'s area inside `b` (same page)."""
    if a[0] != b[0]:
        return 0.0
    w = min(a[3], b[3]) - max(a[1], b[1])
    h = min(a[4], b[4]) - max(a[2], b[2])
    area = (a[3] - a[1]) * (a[4] - a[2])
    return w * h / area if w > 0 and h > 0 and area > 0 else 0.0


def _signature_words(ocr_pages: list[Any], signature_regions: list[dict[str, Any]]) -> list[tuple[str, tuple]]:
    """(text, box) of OCR words that are signature ink."""
    regions = [b for r in signature_regions if r.get("kind", "signature") == "signature" and (b := _box(r))]
    words = []
    for page in ocr_pages or []:
        heights = [w.height for w in page.words if w.text.strip()]
        median = statistics.median(heights) if heights else 0.0
        for w in page.words:
            box = (page.page_number, w.x, w.y, w.x + w.width, w.y + w.height)
            large_handwriting = w.italic_or_handwritten and median and w.height >= _SIGNATURE_HEIGHT_RATIO * median
            if large_handwriting or any(_overlap(box, r) >= 0.5 for r in regions):
                words.append((w.text.strip(), box))
    return words


def strip_signature_text(
    fields: dict[str, Any], ocr_pages: list[Any] | None, signature_regions: list[dict[str, Any]] | None = None
) -> None:
    """Remove signature ink read as words from person-role fields (module
    docstring). Mutates `fields`."""
    ink = _signature_words(ocr_pages or [], signature_regions or [])
    if not ink:
        return
    for field in fields.get("additional_fields") or []:
        if not isinstance(field, dict) or not _PERSON_ROLE_RE.search(field.get("field_name") or ""):
            continue
        value = field.get("value")
        if not isinstance(value, str):
            continue
        field_box = _box(field)
        # A located field drops only the ink inside its own box; otherwise any
        # ink word with the same text.
        ink_texts = {
            t.casefold() for t, box in ink
            if field_box is None or _overlap(box, field_box) > 0 or _overlap(field_box, box) > 0
        }
        tokens = value.split()
        kept = [t for t in tokens if t.casefold().strip(".,:;") not in ink_texts]
        if len(kept) == len(tokens):
            continue
        dropped = [t for t in tokens if t not in kept]
        field["value_as_read"] = value
        field["value"] = " ".join(kept) or None
        field["note"] = (
            f"{', '.join(repr(t) for t in dropped)} left out: read from the handwritten signature, not printed text."
        )
