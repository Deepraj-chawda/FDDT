"""
Locates each extracted field on the page (SPECIFICATION.md section 3.6's template-
free extraction stays as-is; this only adds WHERE each value was read from).

Why this exists: field-level exceptions (amount mismatch, total that doesn't
add up, ...) are rule-based comparisons of normalized values, so they have
no pixel region of their own the way ELA/copy-move findings do. To show
them as highlighted regions the field's position has to be known — and it
used to be discarded: Azure Document Intelligence's Layout result carries a
polygon for every word/line, but app/services/ocr_service.py kept only the
text, and the extraction model (which sees text only) returns values, not
positions.

How it works: the extraction step stays untouched; afterwards each extracted
value is matched back onto the OCR words (which DO have boxes). The box is
stored on the field itself as `bounding_box` — a normalized 0-1 fraction
{page, x, y, width, height}, the same convention as every other check —
next to its normalized value.

This is text matching, so the location is BEST-EFFORT/approximate: when a
value occurs several times (a total that also appears in a summary line) the
nearest keyword context ("total", "subtotal", "VAT", "date", ...) picks the
occurrence, and a field that can't be found simply gets no box — the field
exception is still reported, just without a highlight. Amounts are compared
numerically, so `9,030.00`, `9030` and Arabic-Indic digits all match the
same normalized value.

Pure functions over OCRPage geometry — no I/O, no DB.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date
from typing import Any

from rapidfuzz import fuzz

from app.services.ocr_service import OCRPage

# Arabic-Indic and Eastern Arabic-Indic digits -> ASCII, plus the Arabic
# decimal/thousands separators -> "." / ",".
_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
_EDGE_PUNCT = ".:;,()[]{}\"'«»،؛|"

_AMOUNT_FIELDS = ("amount", "subtotal", "tax_amount", "tax_rate")
# Order matters: fields are placed most-constrained first, and a box already
# taken by an earlier field is avoided by a later one when another candidate
# exists (so a no-tax invoice whose total equals its subtotal doesn't put
# both highlights on the same number).
_PLACEMENT_ORDER = ("amount", "subtotal", "tax_amount", "tax_rate", "date", "reference_number", "issuer")

# Per field: (words whose presence in the same line makes an occurrence more
# likely, words that make it less likely, prefer the LAST occurrence?).
_CONTEXT: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    "amount": (
        ("total", "amount due", "grand", "payable", "balance due", "settled", "paid", "debited",
         "إجمالي", "الإجمالي", "المجموع", "المبلغ", "الاجمالي"),
        ("subtotal", "sub-total", "sub total", "vat", "tax", "unit", "price", "qty", "الفرعي", "ضريبة"),
        True,
    ),
    "subtotal": (("subtotal", "sub-total", "sub total", "الفرعي", "المجموع الفرعي"), ("grand", "due", "payable"), True),
    "tax_amount": (("vat", "tax", "ضريبة", "القيمة المضافة"), ("rate", "subtotal", "total"), True),
    "tax_rate": (("vat", "tax", "%", "ضريبة"), ("total",), True),
    "date": (("date", "issued", "تاريخ"), ("due", "payment", "استحقاق"), False),
    "reference_number": (("invoice", "ref", "no", "number", "#", "رقم", "فاتورة"), (), False),
    "issuer": ((), (), False),
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y")
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_MIN_FUZZY_SCORE = 88


def _norm(text: str) -> str:
    return text.translate(_DIGIT_MAP).casefold().strip(_EDGE_PUNCT)


def parse_number(token: str) -> float | None:
    """A number in whatever separator convention the OCR text used, or None."""
    text = re.sub(r"[^\d.,\-]", "", token.translate(_DIGIT_MAP))
    if not text or not any(c.isdigit() for c in text):
        return None
    if "," in text and "." in text:
        text = (
            text.replace(".", "").replace(",", ".")
            if text.rfind(",") > text.rfind(".")
            else text.replace(",", "")
        )
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = head.replace(",", "") + (tail if len(tail) == 3 else "." + tail)
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class _W:
    text: str
    norm: str
    x: float
    y: float
    w: float
    h: float
    page: int
    context: str = ""


@dataclass
class _Candidate:
    page: int
    x: float
    y: float
    w: float
    h: float
    context: str

    @property
    def key(self) -> tuple[int, float, float]:
        return (self.page, round(self.x, 2), round(self.y, 2))


def _index_words(pages: list[OCRPage]) -> list[list[_W]]:
    indexed: list[list[_W]] = []
    for page in pages:
        words = [_W(w.text, _norm(w.text), w.x, w.y, w.width, w.height, page.page_number) for w in page.words]
        for w in words:
            yc = w.y + w.h / 2
            tolerance = max(w.h, 0.004) * 0.7
            line = [o for o in words if abs((o.y + o.h / 2) - yc) <= tolerance]
            w.context = " ".join(o.text for o in sorted(line, key=lambda o: o.x)).casefold()
        indexed.append(words)
    return indexed


def _union(words: list[_W]) -> _Candidate:
    x0, y0 = min(w.x for w in words), min(w.y for w in words)
    x1, y1 = max(w.x + w.w for w in words), max(w.y + w.h for w in words)
    return _Candidate(words[0].page, x0, y0, x1 - x0, y1 - y0, words[0].context)


def _sequence_matches(words: list[_W], target: str) -> list[_Candidate]:
    """Runs of consecutive words whose normalized text, concatenated, equals
    `target` (also normalized, with spaces removed). Concatenating makes the
    match independent of where the OCR split a value into words."""
    if len(target) < 2:
        return []
    found: list[_Candidate] = []
    for i, start in enumerate(words):
        if not target.startswith(start.norm) or not start.norm:
            continue
        joined, run = "", []
        for word in words[i : i + 14]:
            joined += word.norm
            run.append(word)
            if joined == target:
                found.append(_union(run))
                break
            if not target.startswith(joined):
                break
    return found


def _numeric_matches(words: list[_W], value: float, currency: str | None) -> list[_Candidate]:
    found: list[_Candidate] = []
    for i, word in enumerate(words):
        parsed = parse_number(word.text)
        if parsed is None or abs(parsed - value) > 0.005 or not any(c.isdigit() for c in word.text):
            continue
        run = [word]
        if currency:  # pull an adjacent currency code/symbol into the box
            code = currency.casefold()
            if i + 1 < len(words) and words[i + 1].norm == code:
                run.append(words[i + 1])
            elif i > 0 and words[i - 1].norm == code:
                run.insert(0, words[i - 1])
        found.append(_union(run))
    return found


def _date_targets(field: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    if field.get("raw_text"):
        targets.append(field["raw_text"])
    value = field.get("value")
    if value:
        try:
            parsed = _date.fromisoformat(str(value))
            targets += [parsed.strftime(f) for f in _DATE_FORMATS]
            # strftime zero-pads the day; documents usually don't ("September 3, 2026").
            targets += [
                f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}",
                f"{parsed.day} {parsed.strftime('%B')} {parsed.year}",
            ]
        except ValueError:
            targets.append(str(value))
    return targets


def _flat(text: str) -> str:
    return "".join(_norm(t) for t in text.split())


def _text_candidates(pages_words: list[list[_W]], value: str) -> list[_Candidate]:
    target = _flat(value)
    found: list[_Candidate] = []
    for words in pages_words:
        found += _sequence_matches(words, target)
    return found


def _fuzzy_line(pages: list[OCRPage], value: str) -> _Candidate | None:
    """Best whole line for a long free-text value the exact match missed."""
    needle = _norm(value)
    if len(needle) < 6:
        return None
    best_rank: tuple | None = None
    best: _Candidate | None = None
    for page in pages:
        for line in page.lines:
            haystack = _norm(line.text)
            score = fuzz.partial_ratio(needle, haystack)
            if score < _MIN_FUZZY_SCORE or len(haystack) < 4:
                continue
            # higher score wins; on a tie the topmost line (a letterhead) wins.
            rank = (score, -page.page_number, -line.y)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = _Candidate(page.page_number, line.x, line.y, line.width, line.height, line.text.casefold())
    return best


def _script_segments(value: str) -> list[str]:
    """Split "شركة الأفق Al Ufuq Co." into its Arabic and Latin runs — a
    combined issuer name is usually printed as separate lines."""
    tokens, segments, current, arabic = value.split(), [], [], None
    for token in tokens:
        is_arabic = bool(_ARABIC.search(token))
        if current and is_arabic != arabic:
            segments.append(" ".join(current))
            current = []
        current.append(token)
        arabic = is_arabic
    if current:
        segments.append(" ".join(current))
    return sorted(segments, key=len, reverse=True) if len(segments) > 1 else []


def _pick(candidates: list[_Candidate], field_name: str, used: set) -> _Candidate | None:
    if not candidates:
        return None
    positive, negative, prefer_last = _CONTEXT.get(field_name, ((), (), False))

    def rank(c: _Candidate) -> tuple:
        score = sum(2 for k in positive if k in c.context) - sum(2 for k in negative if k in c.context)
        position = (c.page, c.y) if prefer_last else (-c.page, -c.y)
        return (score, *position)

    ordered = sorted(candidates, key=rank, reverse=True)
    return next((c for c in ordered if c.key not in used), ordered[0])


def _locate(
    pages: list[OCRPage], pages_words: list[list[_W]], name: str, field: dict[str, Any],
    used: set, *, core: bool,
) -> _Candidate | None:
    value = field.get("value")
    if value is None or value == "":
        return None

    if name in _AMOUNT_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
        candidates: list[_Candidate] = []
        for words in pages_words:
            candidates += _numeric_matches(words, float(value), field.get("currency"))
        return _pick(candidates, name, used)

    if name == "date":
        for target in _date_targets(field):
            found = _text_candidates(pages_words, target)
            if found:
                return _pick(found, name, used)
        return None

    text = str(value)
    found = _text_candidates(pages_words, text)
    if found:
        return _pick(found, name, used)
    if not core:
        return None  # additional fields: exact matches only

    if name == "issuer":
        for segment in _script_segments(text):
            found = _text_candidates(pages_words, segment)
            if found:
                return _pick(found, name, used)
    return _fuzzy_line(pages, text)


def _as_box(candidate: _Candidate) -> dict[str, float | int]:
    return {
        "page": candidate.page,
        "x": round(candidate.x, 4),
        "y": round(candidate.y, 4),
        "width": round(candidate.w, 4),
        "height": round(candidate.h, 4),
    }


def attach_field_locations(pages: list[OCRPage], extracted_fields: dict[str, Any]) -> int:
    """Adds `bounding_box` to every field of `extracted_fields` (the stored
    {core_fields, additional_fields} shape) that could be located. Mutates
    in place; returns how many fields got a box. Fields already carrying one
    are left alone."""
    if not pages or not isinstance(extracted_fields, dict):
        return 0
    pages_words = _index_words(pages)
    used: set = set()
    located = 0

    core = extracted_fields.get("core_fields") or {}
    ordered = [n for n in _PLACEMENT_ORDER if n in core] + [n for n in core if n not in _PLACEMENT_ORDER]
    for name in ordered:
        field = core.get(name)
        if not isinstance(field, dict) or field.get("bounding_box"):
            continue
        candidate = _locate(pages, pages_words, name, field, used, core=True)
        if candidate is not None:
            field["bounding_box"] = _as_box(candidate)
            used.add(candidate.key)
            located += 1

    for field in extracted_fields.get("additional_fields") or []:
        if not isinstance(field, dict) or field.get("bounding_box"):
            continue
        candidate = _locate(pages, pages_words, str(field.get("field_name") or ""), field, used, core=False)
        if candidate is not None:
            field["bounding_box"] = _as_box(candidate)
            located += 1

    located += _locate_line_items(pages_words, extracted_fields.get("line_items") or [], used)

    words = extracted_fields.get("amount_in_words")
    if isinstance(words, dict) and words.get("text") and not words.get("bounding_box"):
        candidate = _fuzzy_line(pages, str(words["text"]))
        if candidate is not None:
            words["bounding_box"] = _as_box(candidate)
            located += 1
    return located


def _locate_line_items(pages_words: list[list[_W]], line_items: list[Any], used: set) -> int:
    """Boxes each line item's printed line total. The same figure can appear
    on several lines (two terms at the same fee), so the occurrence whose line
    best matches the item's description and unit price wins, in reading order
    after the previous item."""
    located = 0
    after: tuple[int, float] = (0, -1.0)
    for item in line_items:
        if not isinstance(item, dict) or item.get("bounding_box"):
            continue
        total = item.get("line_total")
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            continue
        candidates = [c for words in pages_words for c in _numeric_matches(words, float(total), None)]
        candidates = [c for c in candidates if c.key not in used]
        if not candidates:
            continue
        description = _norm(str(item.get("description") or ""))
        unit_price = item.get("unit_price")

        def rank(c: _Candidate) -> tuple:
            score = fuzz.partial_ratio(description, c.context) if description else 0
            if isinstance(unit_price, (int, float)) and any(
                (n := parse_number(t)) is not None and abs(n - unit_price) < 0.005 for t in c.context.split()
            ):
                score += 50
            in_order = (c.page, c.y) > after
            return (in_order, score, -c.page, -c.y)

        best = max(candidates, key=rank)
        item["bounding_box"] = _as_box(best)
        used.add(best.key)
        after = (best.page, best.y)
        located += 1
    return located
