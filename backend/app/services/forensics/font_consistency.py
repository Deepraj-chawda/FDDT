"""
Font consistency (SPECIFICATION.md section 3.2 "font inconsistencies").

A business document is produced in one pass by one tool, so text that plays
the same role is set in the same font family. An edited value usually is not:
the editing tool sets it in whatever font it has. This check finds text whose
font family differs from the text of similar size around it — however small
the text is — and puts a box around it.

Each page is read from one of two sources:

  - text layer (digitally produced PDFs): the font NAME of every text run,
    read with PyMuPDF. Exact, so one run in an out-of-place font is enough
    (severity "high").
  - OCR (scanned pages, no text layer): Azure Document Intelligence Layout's
    styleFont add-on, which estimates a similar font family per recognized
    word (app/services/ocr_service.py). An estimate, and a noisy one: it
    labels one body font "Segoe UI" on some words and "Tahoma" on others,
    and guesses wildly on small print. So for OCR, families are compared as
    broad visual classes (_OCR_FONT_CLASSES: humanist sans / grotesque sans /
    serif / mono ...), and only labels at or above _OCR_MIN_CONFIDENCE on
    body-size, upright, printed text are used (_OCR_MIN/MAX_RELATIVE_SIZE of
    the page's median word height; italic/handwritten skipped). Every word
    in an out-of-place class is reported and boxed, even a single one. Its
    severity depends on corroboration: "medium" (scored) once the class is
    out of place on at least _OCR_SCORED_MIN_WORDS words of the page, "low"
    (shown for review, not scored) below that. Calibrated on scans of
    genuine documents (a few stray single-word labels per page, never 5 in
    one class) against scans with retyped amounts (6 and 8 words).

A run is flagged when its family is a MINOR font on the page and it sits among
text of similar size in a more common family:

  - row:    on the same visual row as such text ("Subtotal:" in Helvetica,
            "18,450.00 USD" in Times);
  - column: in a column of runs sharing a left or right edge across rows,
            where most runs are in more common families (a whole line item
            retyped in Courier between line items in DejaVu Sans).

What is deliberately NOT flagged:
  - weight/style (Bold, Italic, ...) and metric-compatible substitutes
    (Arial = Helvetica, Times New Roman = Times, ...);
  - symbol/icon fonts and script fonts used for typed signatures;
  - text of a different size (a letterhead or heading in a display font);
  - text in another writing system (Arabic in an Arabic font beside Latin
    text in a Latin font) — runs are only compared within one script;
  - a family that sets a large share of the page (_DESIGN_SHARE or more of
    its characters), or most of a column of _DESIGN_COLUMN_RUNS or more
    runs: that is the document's design, not an edit.

A page with neither a text layer nor OCR font estimates is reported as not
analysed; fonts there are left to the visual review.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import Any

import pymupdf

from app.services.ocr_service import OCRPage

# Name parts that describe weight/style, not the family ("Arial-BoldMT",
# "Calibri,Bold", "Times-Roman", "DejaVu-Sans-BoldOblique").
_STYLE_PART_RE = re.compile(
    r"^(?:bold|italic|oblique|roman|regular|book|normal|medium|light|semibold|demibold|demi|"
    r"black|heavy|thin|extralight|ultralight|extrabold|ultrabold|it|bd|bi)+$",
    re.IGNORECASE,
)
_VENDOR_SUFFIX_RE = re.compile(r"(?:psmt|mt|ps)$", re.IGNORECASE)
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

# Metric-compatible substitutes: PDF producers and viewers swap these freely,
# so mixing them is not an edit signal.
_FAMILY_ALIASES = {
    "arial": "helvetica", "liberationsans": "helvetica", "nimbussans": "helvetica",
    "nimbussansl": "helvetica", "arimo": "helvetica", "helv": "helvetica",
    "timesnewroman": "times", "liberationserif": "times", "nimbusroman": "times",
    "nimbusromno9l": "times", "tinos": "times", "tiro": "times",
    "couriernew": "courier", "liberationmono": "courier", "nimbusmono": "courier",
    "nimbusmonol": "courier", "cousine": "courier", "cour": "courier",
}

# Not text in the usual sense: bullets/icons, and handwriting-style fonts used
# for typed signatures.
_IGNORED_FAMILY_RE = re.compile(
    r"symbol|dingbat|wingding|webding|awesome|materialicon|icons?$|emoji|"
    r"z003|zapfchancery|script|handwrit|signature|brush",
    re.IGNORECASE,
)
# CSS generic families in Document Intelligence's similarFontFamily lists.
_GENERIC_FAMILIES = {"serif", "sansserif", "monospace", "cursive", "fantasy", "systemui"}

_HAS_ALNUM_RE = re.compile(r"[^\W_]")

# A family setting at least this share of a page's characters (per script) is
# part of the document's design and never flagged.
_DESIGN_SHARE = 0.25
# A column with at least this many runs in one family, most of the column, is
# set in that family by design (every description in Courier); its runs are
# not flagged for mixing with the rest of their row. Kept above the handful of
# values an edit typically touches (three summary amounts retyped together
# must still be caught).
_DESIGN_COLUMN_RUNS = 4
# A column needs this many runs, on as many rows, before it means anything.
_MIN_COLUMN_RUNS = 3

# OCR font estimates below this confidence are ignored.
_OCR_MIN_CONFIDENCE = 0.95
# Only body-size OCR words are compared: between these fractions of the
# page's median word height. Font estimates on small print (footnotes) and on
# large text (headings, logos, signatures in a display face) are unreliable.
_OCR_MIN_RELATIVE_SIZE = 0.8
_OCR_MAX_RELATIVE_SIZE = 1.3
# On an OCR page, an out-of-place font class is scored ("medium") once it
# covers at least this many words; fewer are still shown, as "low".
_OCR_SCORED_MIN_WORDS = 5
# Once this many NUMBERS on a scanned page are scored as one out-of-place
# class, the page's other numbers are re-checked (_numeric_recheck) with the
# font-estimate confidence lowered by 0.15.
_NUMERIC_RECHECK_MIN = 3
_NUMERIC_RECHECK_CONFIDENCE = _OCR_MIN_CONFIDENCE - 0.15
# Document Intelligence family names -> the visual class compared on scans.
# Families not listed here are not used (no basis to compare them).
_OCR_FONT_CLASSES = {
    **dict.fromkeys(
        ["segoeui", "tahoma", "verdana", "calibri", "trebuchetms", "dejavusans", "lucidasans", "opensans",
         "gillsans", "candara", "corbel", "frutiger", "myriadpro", "noto", "notosans", "carlito"],
        "humanist sans",
    ),
    **dict.fromkeys(["helvetica", "roboto", "univers", "franklingothic", "inter"], "grotesque sans"),
    **dict.fromkeys(["centurygothic", "futura", "avenir", "gotham", "montserrat"], "geometric sans"),
    **dict.fromkeys(
        ["times", "timesnew", "georgia", "baskerville", "garamond", "cambria", "bookantiqua", "palatino",
         "palatinolinotype", "constantia", "bodoni", "didot", "caslon", "minion", "minionpro"],
        "serif",
    ),
    **dict.fromkeys(["rockwell", "courierstd"], "slab serif"),
    **dict.fromkeys(["courier", "consolas", "lucidaconsole", "dejavusansmono", "menlo", "monaco"], "monospace"),
}


@dataclass(frozen=True)
class _Source:
    """How runs from one source are compared. Tolerances are in points or,
    with `_rel`, as a fraction of the page's median text height (a scan is
    noisy and often slightly skewed)."""

    name: str
    severity: str
    row_tolerance_rel: float | None  # None: fixed _pt value
    row_tolerance_pt: float
    column_tolerance_rel: float | None
    column_tolerance_pt: float
    size_ratio: float
    # Out-of-place words of one family needed for `severity`; below that a
    # finding is still reported, as "low".
    corroborating_words: int


TEXT_LAYER = _Source("text_layer", "high", None, 2.0, None, 1.5, 1.15, 1)
OCR = _Source("ocr", "medium", 0.45, 0.0, 0.35, 0.0, 1.5, _OCR_SCORED_MIN_WORDS)


def font_family(font_name: str) -> str:
    """Normalized family of a PDF font name, ignoring subset tag, weight,
    style, vendor suffix and metric-compatible aliases."""
    name = _SUBSET_PREFIX_RE.sub("", font_name or "").strip()
    parts = [p for p in re.split(r"[-,_ ]+", name) if p]
    kept: list[str] = []
    for i, part in enumerate(parts):
        bare = _VENDOR_SUFFIX_RE.sub("", part) or part
        if i > 0 and _STYLE_PART_RE.match(bare):
            continue
        kept.append(bare)
    family = "".join(kept).lower()
    family = _VENDOR_SUFFIX_RE.sub("", family) or family
    # Style glued onto the family with no separator ("ArialBold", "TimesBoldItalic").
    family = re.sub(r"(?:bold|italic|oblique|regular)+$", "", family) or family
    return _FAMILY_ALIASES.get(family, family)


def ocr_font_class(similar_font_family: str | None) -> str | None:
    """Visual class of Document Intelligence's similarFontFamily list
    ("Segoe UI, Tahoma, sans-serif"), from its first named (non-generic)
    family; None when that family is not a known one."""
    for candidate in (similar_font_family or "").split(","):
        name = candidate.strip().strip("'\"")
        if name and re.sub(r"[^a-z]", "", name.lower()) not in _GENERIC_FAMILIES:
            return _OCR_FONT_CLASSES.get(font_family(name))
    return None


def ocr_font_classes(similar_font_family: str | None) -> set[str]:
    """Every visual class among ALL the named families in Document
    Intelligence's similarFontFamily list, not just the first."""
    classes = set()
    for candidate in (similar_font_family or "").split(","):
        name = re.sub(r"[^a-z]", "", candidate.strip().strip("'\"").lower())
        if name and name not in _GENERIC_FAMILIES and (cls := _OCR_FONT_CLASSES.get(font_family(candidate.strip()))):
            classes.add(cls)
    return classes


def _script(text: str) -> str:
    """Writing system of a run: the first non-Latin script among its letters,
    else "latin" (digits and punctuation count as Latin)."""
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script in ("ARABIC", "HEBREW", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "DEVANAGARI", "THAI",
                       "CYRILLIC", "GREEK"):
            if name.startswith(script):
                return script.lower()
    return "latin"


@dataclass(eq=False)
class _Run:
    page: int  # 1-based
    text: str
    font: str  # as reported (PDF font name, or OCR similarFontFamily)
    family: str
    script: str
    size: float
    bbox: tuple[float, float, float, float]  # points
    baseline: float

    @property
    def chars(self) -> int:
        return len(self.text)


def _keep(text: str, family: str | None) -> bool:
    return bool(text and _HAS_ALNUM_RE.search(text) and family and not _IGNORED_FAMILY_RE.search(family))


def _text_layer_runs(page: pymupdf.Page, page_number: int) -> list[_Run]:
    runs: list[_Run] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            # Rotated text (watermarks, side notes) is laid out differently;
            # rows/columns only make sense for horizontal text.
            if abs(line.get("dir", (1, 0))[1]) > 0.01:
                continue
            for span in line["spans"]:
                text = span["text"].strip()
                family = font_family(span["font"])
                if not _keep(text, family):
                    continue
                runs.append(
                    _Run(
                        page=page_number,
                        text=text,
                        font=_SUBSET_PREFIX_RE.sub("", span["font"]),
                        family=family,
                        script=_script(text),
                        size=round(span["size"], 1),
                        bbox=tuple(span["bbox"]),
                        baseline=span["origin"][1],
                    )
                )
    return runs


def _ocr_runs(ocr_page: OCRPage, page_rect: pymupdf.Rect) -> list[_Run]:
    width, height = page_rect.width, page_rect.height
    # Body size is judged against ALL recognized words, not just the ones
    # with a confident font label (those skew small on some pages).
    heights = [w.height * height for w in ocr_page.words if w.text.strip()]
    median = statistics.median(heights) if heights else 0.0
    min_size, max_size = _OCR_MIN_RELATIVE_SIZE * median, _OCR_MAX_RELATIVE_SIZE * median
    runs: list[_Run] = []
    for word in ocr_page.words:
        if (
            (word.font_confidence or 0) < _OCR_MIN_CONFIDENCE
            or not min_size <= word.height * height <= max_size
            or word.italic_or_handwritten  # signatures; their font labels are guesses
        ):
            continue
        text = word.text.strip()
        family = ocr_font_class(word.font_family)
        if not _keep(text, family):
            continue
        x0, y0 = word.x * width, word.y * height
        x1, y1 = x0 + word.width * width, y0 + word.height * height
        runs.append(
            _Run(
                page=ocr_page.page_number,
                text=text,
                font=word.font_family or "",
                family=family,
                script=_script(text),
                size=round(y1 - y0, 1),
                bbox=(x0, y0, x1, y1),
                baseline=y1,
            )
        )
    return runs


def _group(runs: list[_Run], key, tolerance: float) -> list[list[_Run]]:
    groups: list[list[_Run]] = []
    for run in sorted(runs, key=key):
        if groups and key(run) - key(groups[-1][-1]) <= tolerance:
            groups[-1].append(run)
        else:
            groups.append([run])
    return groups


def _script_outliers(runs: list[_Run], source: _Source) -> list[tuple[_Run, str, int]]:
    """(run, expected family, how many runs of its family are out of place)
    for each run in a minor family that sits among more common families.
    `runs` are one page, one script."""
    share: dict[str, int] = {}
    for r in runs:
        share[r.family] = share.get(r.family, 0) + r.chars
    total = sum(share.values())
    minor = {f for f, n in share.items() if n < total * _DESIGN_SHARE}
    if not minor:
        return []

    median_size = statistics.median(r.size for r in runs) or 1.0
    row_tol = source.row_tolerance_pt if source.row_tolerance_rel is None else source.row_tolerance_rel * median_size
    col_tol = (
        source.column_tolerance_pt if source.column_tolerance_rel is None else source.column_tolerance_rel * median_size
    )

    def same_size(a: _Run, b: _Run) -> bool:
        small, large = sorted((a.size, b.size))
        return small > 0 and large / small <= source.size_ratio

    def more_common(r: _Run, others: list[_Run]) -> list[_Run]:
        return [o for o in others if o is not r and same_size(r, o) and share[o.family] > share[r.family]]

    def expected(peers: list[_Run]) -> str:
        return max({p.family for p in peers}, key=lambda f: share[f])

    columns: list[list[_Run]] = []
    for edge in (lambda r: r.bbox[0], lambda r: r.bbox[2]):
        for group in _group(runs, edge, col_tol):
            rows = {round(r.baseline / max(row_tol, 0.1)) for r in group}
            if len(group) >= _MIN_COLUMN_RUNS and len(rows) >= _MIN_COLUMN_RUNS:
                columns.append(group)

    design_runs: set[int] = set()
    for column in columns:
        for family in {r.family for r in column}:
            members = [r for r in column if r.family == family]
            if len(members) >= _DESIGN_COLUMN_RUNS and len(members) * 2 > len(column):
                design_runs.update(id(r) for r in members)

    found: dict[int, tuple[_Run, str]] = {}
    for row in _group(runs, lambda r: r.baseline, row_tol):
        for r in row:
            if r.family in minor and id(r) not in design_runs and (peers := more_common(r, row)):
                found[id(r)] = (r, expected(peers))
    for column in columns:
        for r in column:
            if r.family not in minor or id(r) in found:
                continue
            peers = more_common(r, column)
            same = [o for o in column if o is not r and same_size(r, o) and o.family == r.family]
            if len(peers) > len(same) + 1:
                found[id(r)] = (r, expected(peers))

    per_family: dict[str, int] = {}
    for r, _ in found.values():
        per_family[r.family] = per_family.get(r.family, 0) + 1
    return [(r, e, per_family[r.family]) for r, e in found.values()]


_NUMERIC_RE = re.compile(r"^[^\w]*\d[\d.,/:\-]*[^\w]*$")
# An amount as printed: "6,000", "57,182.50", "42.50" — not a reference or
# invoice number ("10139/"), which is only ever re-checked as part of the
# original analysis.
_AMOUNT_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d{1,3})?$|^\d+\.\d{2}$")


def _is_numeric(text: str) -> bool:
    return bool(_NUMERIC_RE.match(text))


def _numeric_recheck(
    ocr_page: OCRPage, page_rect: pymupdf.Rect, outliers: list[tuple[_Run, str, int]]
) -> list[tuple[_Run, str, int]]:
    """Amounts retyped in another font usually all are: once at least
    _NUMERIC_RECHECK_MIN numbers on a scanned page are scored as one
    out-of-place class, the page's OTHER numbers are looked at again, more
    leniently — a font estimate down to _NUMERIC_RECHECK_CONFIDENCE, and that
    class anywhere in the estimate's list of similar families (OCR sometimes
    lists the body font first: "Segoe UI, Arial, Helvetica" for an Arial
    figure). Amounts only ("6,000", "42.50"): words and reference numbers are
    never re-checked, so documents without edited amounts get no extra noise. Returns the additional
    outliers, with the same expected family and an updated count."""
    scored_numeric: dict[str, list[tuple[_Run, str, int]]] = {}
    for run, expected, count in outliers:
        if count >= OCR.corroborating_words and _is_numeric(run.text):
            scored_numeric.setdefault(run.family, []).append((run, expected, count))
    targets = {family: items for family, items in scored_numeric.items() if len(items) >= _NUMERIC_RECHECK_MIN}
    if not targets:
        return []

    width, height = page_rect.width, page_rect.height
    heights = [w.height * height for w in ocr_page.words if w.text.strip()]
    median = statistics.median(heights) if heights else 0.0
    flagged_boxes = {tuple(round(v, 1) for v in run.bbox) for run, _, _ in outliers}
    extra: list[tuple[_Run, str, int]] = []
    for family, items in targets.items():
        expected = max({e for _, e, _ in items}, key=lambda e: sum(1 for _, x, _ in items if x == e))
        found: list[_Run] = []
        for word in ocr_page.words:
            text = word.text.strip()
            box = (word.x * width, word.y * height, (word.x + word.width) * width, (word.y + word.height) * height)
            if (
                not _AMOUNT_RE.match(text)
                or tuple(round(v, 1) for v in box) in flagged_boxes
                or (word.font_confidence or 0) < _NUMERIC_RECHECK_CONFIDENCE
                or word.italic_or_handwritten
                or not _OCR_MIN_RELATIVE_SIZE * median <= word.height * height <= _OCR_MAX_RELATIVE_SIZE * median
                or family not in ocr_font_classes(word.font_family)
            ):
                continue
            found.append(
                _Run(
                    page=ocr_page.page_number, text=text, font=word.font_family or "", family=family,
                    script=_script(text), size=round(box[3] - box[1], 1), bbox=box, baseline=box[3],
                )
            )
        count = items[0][2] + len(found)
        extra.extend((run, expected, count) for run in found)
    return extra


def _normalized_box(run: _Run, page_rect: pymupdf.Rect, pad: float = 1.5) -> dict[str, Any]:
    width, height = page_rect.width or 1, page_rect.height or 1
    x0, y0, x1, y1 = run.bbox
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, width), min(y1 + pad, height)
    return {
        "page": run.page,
        "x": round(x0 / width, 5),
        "y": round(y0 / height, 5),
        "width": round((x1 - x0) / width, 5),
        "height": round((y1 - y0) / height, 5),
    }


def _finding(
    run: _Run, expected: str, family_count: int, source: _Source, page_rect: pymupdf.Rect, *, rechecked: bool = False
) -> dict[str, Any]:
    corroborated = family_count >= source.corroborating_words
    if source is TEXT_LAYER:
        description = (
            f"Page {run.page}: '{run.text}' is set in {run.font}, while the text around it uses the "
            f"{expected} font family — it may have been typed in after the document was produced."
        )
    else:
        description = (
            f"Page {run.page} (scanned): '{run.text}' looks like a different font ({run.family}) from the "
            f"text around it ({expected}), per OCR font recognition — it may have been typed in or "
            "pasted before the page was printed or scanned."
        )
        if rechecked:
            description += (
                f" Found on a second look at this page's numbers: OCR lists {run.family} among the likely fonts "
                f"for it ({run.font}), as for the other amounts flagged here."
            )
        if not corroborated:
            description += (
                f" Only {family_count} word(s) on this page look like this font, so it is shown for review but "
                "not scored: OCR font recognition alone is not reliable on a word or two."
            )
    return {
        "finding": "font_inconsistency",
        "severity": source.severity if corroborated else "low",
        "description": description,
        "page": run.page,
        "bounding_box": _normalized_box(run, page_rect),
        "data": {
            "text": run.text,
            "font": run.font,
            "family": run.family,
            "expected_family": expected,
            "font_size": run.size,
            "source": source.name,
            "words_in_this_font": family_count,
            **({"numeric_recheck": True} if rechecked else {}),
        },
    }


def analyze_font_consistency(pdf_bytes: bytes, ocr_pages: list[OCRPage] | None = None) -> dict[str, Any]:
    """Runs the check over one PDF. `ocr_pages` (with styleFont estimates) are
    used for pages that have no text layer. Returns {"result": "pass"|"flag",
    "details": [finding, ...]} for a `document_checks.result` jsonb column:
    one `font_family_inventory` (info) plus one `font_inconsistency` per text
    run set in an out-of-place font, with its page and bounding box."""
    ocr_by_page = {p.page_number: p for p in ocr_pages or []}
    findings: list[dict[str, Any]] = []
    inventory: dict[str, int] = {}
    page_sources: list[dict[str, Any]] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        # An encrypted file has no readable text layer (upload validation
        # normally refuses these before any check runs).
        pages = [] if doc.needs_pass else doc
        for index, page in enumerate(pages):
            number = index + 1
            source, runs = TEXT_LAYER, _text_layer_runs(page, number)
            if not runs and number in ocr_by_page:
                source, runs = OCR, _ocr_runs(ocr_by_page[number], page.rect)
            page_sources.append({"page": number, "source": source.name if runs else "none"})
            if not runs:
                continue
            by_script: dict[str, list[_Run]] = {}
            for r in runs:
                inventory[r.family] = inventory.get(r.family, 0) + r.chars
                by_script.setdefault(r.script, []).append(r)
            outliers = [o for group in by_script.values() for o in _script_outliers(group, source)]
            rechecked: set[int] = set()
            if source is OCR:
                extra = _numeric_recheck(ocr_by_page[number], page.rect, outliers)
                if extra:
                    rechecked = {id(run) for run, _, _ in extra}
                    totals = {run.family: count for run, _, count in extra}
                    outliers = [
                        (run, expected, totals.get(run.family, count)) for run, expected, count in outliers
                    ] + extra
            outliers.sort(key=lambda o: (o[0].baseline, o[0].bbox[0]))
            findings.extend(
                _finding(run, expected, count, source, page.rect, rechecked=id(run) in rechecked)
                for run, expected, count in outliers
            )
    finally:
        doc.close()

    families = sorted(inventory, key=lambda f: -inventory[f])
    analysed = [p for p in page_sources if p["source"] != "none"]
    if not analysed:
        description = (
            "No text layer or OCR font information to analyse — fonts on this document are covered only "
            "by the visual review."
        )
    else:
        scanned = sum(p["source"] == "ocr" for p in analysed)
        description = (
            f"{len(analysed)} of {len(page_sources)} page(s) analysed"
            + (f" ({scanned} scanned, using OCR font recognition)" if scanned else "")
            + f"; {len(families)} font famil{'y' if len(families) == 1 else 'ies'}: {', '.join(families)}."
        )
    findings.insert(
        0,
        {
            "finding": "font_family_inventory",
            "severity": "info",
            "description": description,
            "data": {"families": {f: inventory[f] for f in families}, "pages": page_sources},
        },
    )
    flagged = any(f["severity"] in ("medium", "high") for f in findings)
    return {"result": "flag" if flagged else "pass", "details": findings}
