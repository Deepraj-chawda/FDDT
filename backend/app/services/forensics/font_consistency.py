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

Scans converted to editable text (Acrobat "Edit scanned document" and the
like) are judged differently. The converter re-creates every word as live
text in subset fonts it generates per guessed style, named "<Family>-NNNN"
("Comic Sans MS-Bold-6003", "Arial-Bold-6000"); its guesses differ from word
to word on small print, so two such subsets side by side are NOT evidence.
What is: a character in a FULL font (no -NNNN suffix) on the same row as the
converter's subsets — typed in after the conversion. On a page whose text is
mostly in such subsets (_OCR_SUBSET_PAGE_SHARE), only that is flagged
(severity "high"), and the finding lists which digits the neighbouring
subset holds: a subset only contains the glyphs originally printed there, so
it hints at what the original value was.

Separately, on every text-layer page, a number whose characters are set at
sizes differing by _NUMBER_SIZE_STEP_PT or more ("36,000" with the "6" and
"000" at 11.7 pt inside a 12 pt figure) is flagged (`font_size_inconsistency`,
"high"), or noted on the font finding that already covers those characters.

Also on every text-layer page: a font embedded more than once. A document
exported in one pass embeds each font (and style) once, as one subset tagged
"ABCDEF+"; an edit session that types text in adds a further subset of the
same font. Text in such an extra subset is flagged (`font_subset_split`,
"high") when it is on the same line as text in the main subset, or is a
number in a column whose other numbers are in the main subset, or is an
amount in an extra subset holding only digits and punctuation ("1,450.00"
in a copy of Times New Roman that has only ", . 0 1 2 3 4 5 6"). An extra
subset by itself is not enough: Word, for one, puts a stray en-dash or a
second bold-italic heading in a subset of its own.
"""
from __future__ import annotations

import re
import statistics
import threading
import unicodedata
from contextlib import contextmanager
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
# "ABCDEF+Arial" (standard subset tag) and "*Arial" (how PyMuPDF names some
# CID fonts).
_SUBSET_PREFIX_RE = re.compile(r"^(?:[A-Z]{6}\+|\*)")
# The per-style subset fonts a scan-to-editable-text converter generates:
# "Comic Sans MS-Bold-6003", "Times New Roman-5999".
_OCR_SUBSET_SUFFIX_RE = re.compile(r"-\d{4}$")
# A text-layer page with at least this share of its characters in such
# subsets is a converted scan (module docstring).
_OCR_SUBSET_PAGE_SHARE = 0.6
# Characters of one number set this far apart in size (points) were not
# set together.
_NUMBER_SIZE_STEP_PT = 0.2
_NUMBER_TOKEN_RE = re.compile(r"^\d[\d.,/:\-]*\d$|^\d$")

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
    style, vendor suffix, a converter's -NNNN subset number and
    metric-compatible aliases."""
    name = _OCR_SUBSET_SUFFIX_RE.sub("", _SUBSET_PREFIX_RE.sub("", font_name or "").strip())
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


def is_ocr_subset_font(font_name: str) -> bool:
    """A subset font generated by a scan-to-editable-text converter
    ("Comic Sans MS-Bold-6003"), as opposed to a full font."""
    return bool(_OCR_SUBSET_SUFFIX_RE.search(_SUBSET_PREFIX_RE.sub("", font_name or "").strip()))


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


# --- scans converted to editable text ---------------------------------------------

_BFCHAR_RE = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
_BFRANGE_RE = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
_HEX_PAIR_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_HEX_RANGE_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")


def _unicode_of(hex_bytes: bytes) -> str:
    try:
        return bytes.fromhex(hex_bytes.decode()).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return ""


def _to_unicode_chars(cmap: bytes) -> set[str]:
    """Every character a font's ToUnicode CMap maps to: the glyphs it holds."""
    chars: set[str] = set()
    for block in _BFCHAR_RE.findall(cmap):
        for _, dst in _HEX_PAIR_RE.findall(block):
            chars.update(_unicode_of(dst))
    for block in _BFRANGE_RE.findall(cmap):
        for lo, hi, dst in _HEX_RANGE_RE.findall(block):
            start = _unicode_of(dst)
            if len(start) == 1:
                span = min(int(hi, 16) - int(lo, 16), 0xFFFF)
                chars.update(chr(ord(start) + i) for i in range(span + 1))
    return chars


def _subset_charsets(doc: pymupdf.Document, page: pymupdf.Page) -> dict[str, set[str]]:
    """The glyphs held by each converter-generated subset font on the page,
    keyed by font name as reported on text spans."""
    charsets: dict[str, set[str]] = {}
    for xref, _ext, _type, basefont, *_ in page.get_fonts(full=True):
        name = _SUBSET_PREFIX_RE.sub("", re.sub(r"-Identity-[HV]$", "", basefont or ""))
        if not is_ocr_subset_font(name):
            continue
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        try:
            charsets[name] = _to_unicode_chars(doc.xref_stream(int(value.split()[0])) or b"")
        except (ValueError, RuntimeError):
            continue
    return charsets


def _subset_share(runs: list[_Run]) -> float:
    total = sum(r.chars for r in runs)
    return sum(r.chars for r in runs if is_ocr_subset_font(r.font)) / total if total else 0.0


def _converted_scan_findings(
    runs: list[_Run], charsets: dict[str, set[str]], page_rect: pymupdf.Rect
) -> list[tuple[dict[str, Any], _Run]]:
    """On a scan converted to editable text: (finding, run) for each run in a
    full font on a row of the converter's subset fonts of similar size."""
    def similar(a: _Run, b: _Run) -> bool:
        small, large = sorted((a.size, b.size))
        return small > 0 and large / small <= TEXT_LAYER.size_ratio

    typed = [r for r in runs if not is_ocr_subset_font(r.font)]
    found: list[tuple[_Run, _Run]] = []
    for row in _group(runs, lambda r: r.baseline, TEXT_LAYER.row_tolerance_pt):
        peers = [p for p in row if is_ocr_subset_font(p.font)]
        for r in row:
            if r in typed and (near := [p for p in peers if similar(r, p)]):
                nearest = min(near, key=lambda p: min(abs(p.bbox[0] - r.bbox[2]), abs(r.bbox[0] - p.bbox[2])))
                found.append((r, nearest))
    per_font: dict[str, int] = {}
    for r, _ in found:
        per_font[r.font] = per_font.get(r.font, 0) + 1

    findings = []
    for run, peer in found:
        description = (
            f"Page {run.page}: '{run.text}' is set in the full font {run.font} ({run.size:g} pt), while the rest of "
            f"its line uses {peer.font} ({peer.size:g} pt) — a subset font generated when this scan was converted "
            "to editable text. Characters in a full font among the converter's subsets were typed in after the "
            "conversion."
        )
        data: dict[str, Any] = {
            "text": run.text,
            "font": run.font,
            "family": run.family,
            "expected_family": peer.family,
            "expected_font": peer.font,
            "font_size": run.size,
            "expected_font_size": peer.size,
            "source": TEXT_LAYER.name,
            "words_in_this_font": per_font[run.font],
            "converted_scan": True,
        }
        original_digits = sorted(c for c in charsets.get(peer.font, set()) if c.isdigit())
        missing = sorted({c for c in run.text if c.isdigit()} - set(original_digits))
        if peer.font in charsets and missing:
            held = ", ".join(original_digits) if original_digits else "no digits"
            description += (
                f" {peer.font} holds only the glyphs originally printed with it — digits: {held} — so the "
                f"original value on this line contained no {' or '.join(missing)}."
            )
            data["original_subset_digits"] = original_digits
            data["digits_not_in_original"] = missing
        findings.append(
            (
                {
                    "finding": "font_inconsistency",
                    "severity": TEXT_LAYER.severity,
                    "description": description,
                    "page": run.page,
                    "bounding_box": _normalized_box(run, page_rect),
                    "data": data,
                },
                run,
            )
        )
    return findings


# --- size changes inside one number --------------------------------------------------


_SUBSET_TAG_RE = re.compile(r"^([A-Z]{6})\+(.+)$")
_STYLE_WORD_RE = re.compile(r"bold|italic|oblique|black|heavy|semibold|demibold|light|medium|condensed", re.IGNORECASE)
# Spans sharing a right (or left) edge within this many points are a column.
_COLUMN_EDGE_PT = 2.0
_DIGITS_PUNCTUATION = set("0123456789.,:/-()%+ ")


_SUBSET_TAGS_LOCK = threading.Lock()


@contextmanager
def _subset_tags():
    """PyMuPDF reports font names without their "ABCDEF+" subset tag unless
    asked; the tag is what tells two embedded copies of one font apart. The
    setting is process-wide, so it is switched under a lock (workers run
    checks in threads); other readers of font names strip the tag anyway."""
    with _SUBSET_TAGS_LOCK:
        previous = pymupdf.TOOLS.set_subset_fontnames()
        pymupdf.TOOLS.set_subset_fontnames(True)
        try:
            yield
        finally:
            pymupdf.TOOLS.set_subset_fontnames(bool(previous))


@dataclass(eq=False)
class _TaggedSpan:
    text: str
    tag: str
    base: str  # font name without the tag
    bbox: tuple[float, float, float, float]

    @property
    def amount(self) -> bool:
        return bool(_AMOUNT_RE.match(self.text))


def _style(font_name: str) -> str:
    return "".join(sorted({m.lower() for m in _STYLE_WORD_RE.findall(font_name)}))


def _tagged_spans(page: pymupdf.Page) -> list[_TaggedSpan]:
    with _subset_tags():
        blocks = page.get_text("dict")["blocks"]
    spans = []
    for block in blocks:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                match = _SUBSET_TAG_RE.match(span["font"])
                if match and _HAS_ALNUM_RE.search(text):
                    spans.append(_TaggedSpan(text, match.group(1), match.group(2), tuple(span["bbox"])))
    return spans


def _same_row(a: _TaggedSpan, b: _TaggedSpan) -> bool:
    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    return overlap >= 0.5 * min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])


def _same_column(a: _TaggedSpan, b: _TaggedSpan) -> bool:
    return not _same_row(a, b) and (
        abs(a.bbox[2] - b.bbox[2]) <= _COLUMN_EDGE_PT or abs(a.bbox[0] - b.bbox[0]) <= _COLUMN_EDGE_PT
    )


def _subset_split_findings(page: pymupdf.Page, page_number: int) -> list[dict[str, Any]]:
    """`font_subset_split` findings (module docstring): text set in a second
    embedded copy (subset) of a font the page already embeds, that shares a
    row with the main copy, or is a number in a column the main copy also
    sets numbers in, or is an amount in a copy that holds only digits and
    punctuation."""
    groups: dict[tuple[str, str], dict[str, list[_TaggedSpan]]] = {}
    for span in _tagged_spans(page):
        key = (font_family(span.base), _style(span.base))
        groups.setdefault(key, {}).setdefault(span.tag, []).append(span)
    findings = []
    for subsets in groups.values():
        if len(subsets) < 2:
            continue
        main_tag = max(subsets, key=lambda t: sum(len(s.text) for s in subsets[t]))
        main = subsets[main_tag]
        for tag, spans in subsets.items():
            if tag == main_tag:
                continue
            glyphs = sorted({ch for s in spans for ch in s.text if not ch.isspace()})
            digits_only = set(glyphs) <= _DIGITS_PUNCTUATION
            for span in spans:
                row = next((m for m in main if _same_row(span, m)), None)
                column = next((m for m in main if span.amount and m.amount and _same_column(span, m)), None)
                amounts_only = digits_only and span.amount
                if not (row or column or amounts_only):
                    continue
                reasons = []
                if amounts_only:
                    reasons.append(f"this copy holds only the characters {' '.join(glyphs)}")
                if column is not None:
                    reasons.append(f"the same column's '{column.text}' is set in the main copy")
                if row is not None:
                    reasons.append(f"'{row.text}' on the same line is set in the main copy")
                findings.append(
                    {
                        "finding": "font_subset_split",
                        "severity": "high",
                        "description": (
                            f"Page {page_number}: '{span.text}' is set in a second embedded copy of {span.base} "
                            f"(subset {tag}; the page's main copy is {main_tag}) — " + "; ".join(reasons) + ". "
                            "A document exported in one pass embeds each font once; a further copy of the same "
                            "font appears when text is typed in during a later edit."
                        ),
                        "page": page_number,
                        "bbox": span.bbox,
                        "data": {
                            "text": span.text,
                            "font": span.base,
                            "subset": tag,
                            "main_subset": main_tag,
                            "subset_glyphs": "".join(glyphs),
                            "digits_only_subset": digits_only,
                            "same_row_as": row.text if row else None,
                            "same_column_as": column.text if column else None,
                            "source": TEXT_LAYER.name,
                        },
                    }
                )
    return findings


def _number_size_steps(page: pymupdf.Page, page_number: int) -> list[dict[str, Any]]:
    """Numbers on a text-layer page whose characters are set at sizes
    _NUMBER_SIZE_STEP_PT or more apart. Each: page, text, bbox, the sizes."""
    steps: list[dict[str, Any]] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[1]) > 0.01:
                continue
            chars = [(c["c"], span["size"], c["bbox"]) for span in line["spans"] for c in span["chars"]]
            token: list[tuple[str, float, tuple]] = []
            for ch in chars + [(" ", 0.0, (0, 0, 0, 0))]:
                if not ch[0].isspace():
                    token.append(ch)
                    continue
                text = "".join(c for c, _, _ in token)
                sizes = [s for _, s, _ in token]
                if token and _NUMBER_TOKEN_RE.match(text) and max(sizes) - min(sizes) >= _NUMBER_SIZE_STEP_PT:
                    steps.append(
                        {
                            "page": page_number,
                            "text": text,
                            "bbox": (
                                min(b[0] for _, _, b in token), min(b[1] for _, _, b in token),
                                max(b[2] for _, _, b in token), max(b[3] for _, _, b in token),
                            ),
                            "sizes": sorted({round(v, 1) for v in sizes}),
                        }
                    )
                token = []
    return steps


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _apply_size_steps(
    steps: list[dict[str, Any]], runs_by_finding: list[tuple[dict[str, Any], _Run]], page_rect: pymupdf.Rect
) -> list[dict[str, Any]]:
    """Notes each size step on the font findings inside that number; returns
    new findings for steps no font finding covers."""
    new: list[dict[str, Any]] = []
    for step in steps:
        sizes = " vs ".join(f"{s:g} pt" for s in step["sizes"])
        note = f"The number '{step['text']}' mixes character sizes ({sizes})."
        covering = [f for f, run in runs_by_finding if run.page == step["page"] and _overlaps(run.bbox, step["bbox"])]
        for f in covering:
            if note not in f["description"]:
                f["description"] += " " + note
                f["data"]["size_change"] = {"number": step["text"], "sizes": step["sizes"]}
        if covering:
            continue
        box_run = _Run(step["page"], step["text"], "", "", "latin", 0.0, step["bbox"], step["bbox"][3])
        new.append(
            {
                "finding": "font_size_inconsistency",
                "severity": TEXT_LAYER.severity,
                "description": (
                    f"Page {step['page']}: {note} Characters of one number set at different sizes were not "
                    "typed together — some may have been replaced."
                ),
                "page": step["page"],
                "bounding_box": _normalized_box(box_run, page_rect),
                "data": {"text": step["text"], "sizes": step["sizes"], "source": TEXT_LAYER.name},
            }
        )
    return new


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
            converted = source is TEXT_LAYER and _subset_share(runs) >= _OCR_SUBSET_PAGE_SHARE
            page_sources.append(
                {"page": number, "source": source.name if runs else "none", **({"converted_scan": True} if converted else {})}
            )
            if not runs:
                continue
            by_script: dict[str, list[_Run]] = {}
            for r in runs:
                inventory[r.family] = inventory.get(r.family, 0) + r.chars
                by_script.setdefault(r.script, []).append(r)
            steps = _number_size_steps(page, number) if source is TEXT_LAYER else []
            if source is TEXT_LAYER:
                for split in _subset_split_findings(page, number):
                    box = split.pop("bbox")
                    run = _Run(number, split["data"]["text"], split["data"]["font"], "", "latin", 0.0, box, box[3])
                    findings.append({**split, "bounding_box": _normalized_box(run, page.rect)})
            if converted:
                pairs = _converted_scan_findings(runs, _subset_charsets(doc, page), page.rect)
                findings.extend(f for f, _ in pairs)
                findings.extend(_apply_size_steps(steps, pairs, page.rect))
                continue
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
            pairs = [
                (_finding(run, expected, count, source, page.rect, rechecked=id(run) in rechecked), run)
                for run, expected, count in outliers
            ]
            findings.extend(f for f, _ in pairs)
            findings.extend(_apply_size_steps(steps, pairs, page.rect))
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
