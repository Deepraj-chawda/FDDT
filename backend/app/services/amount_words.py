"""
Reads an amount written out in English words ("Twenty Thousand Five Hundred
AED Only", "one hundred and five dollars and fifty cents") as a number.

Used by the amount-in-words check (app/services/field_validation_service.py)
so that comparison does not rest on the extraction model's own reading of the
words: a model asked to convert words that contradict the figures beside them
tends to "fix" them. Anything this cannot read with certainty (other
languages, digits mixed in, unknown words, two phrases run together) returns
None, and the caller falls back to the extraction model's value.

Also here: `find_amount_words` (the words, their label and any figure printed
in the same line or cell), `parse_figure` (a printed number), `amount_kind`
(which figure a label such as "VAT Amount in Words" names) — the vocabulary
app/services/amount_words_layout.py uses to pair each phrase with its figure.
"""
from __future__ import annotations

import re

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_SCALES = {"thousand": 1_000, "lakh": 100_000, "lac": 100_000, "million": 1_000_000, "crore": 10_000_000,
           "billion": 1_000_000_000}
# Currency names and filler that carry no number.
_MAJOR = {
    "dirham", "dirhams", "aed", "dollar", "dollars", "usd", "riyal", "riyals", "rial", "rials", "sar", "qar",
    "omr", "dinar", "dinars", "kwd", "bhd", "jod", "pound", "pounds", "gbp", "euro", "euros", "eur", "rupee",
    "rupees", "inr", "us", "uae", "saudi", "qatari", "omani", "kuwaiti", "bahraini",
}
_MINOR = {"fils", "cent", "cents", "halala", "halalas", "baisa", "baiza", "paise", "pence", "penny"}
_FILLER = {"only", "and", "a", "of", "the", "amount", "total", "sum", "say", "in", "words", "word", "point"}


def _integer(words: list[str]) -> int | None:
    """The integer a well-formed English number phrase spells. Anything that
    is not ONE number — two phrases run together ("twenty nine thousand six
    hundred one thousand four hundred eighty"), a scale repeated or out of
    order, two tens words in a row — returns None rather than a sum: the
    caller must not report a figure the document never printed."""
    total, current, seen = 0, 0, False
    last_scale = None  # the scale words must descend: million, thousand, (end)
    last = None  # the kind of the previous word within the current group
    for word in words:
        if word in _UNITS:
            if last in ("unit", "teen") or (last == "tens" and _UNITS[word] in (0, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19)):
                return None
            current += _UNITS[word]
            last = "teen" if _UNITS[word] >= 10 or _UNITS[word] == 0 else "unit"
        elif word in _TENS:
            if last in ("tens", "unit", "teen"):
                return None
            current += _TENS[word]
            last = "tens"
        elif word == "hundred":
            if last == "hundred" or current >= 100:
                return None
            current = (current or 1) * 100
            last = "hundred"
        elif word in _SCALES:
            scale = _SCALES[word]
            if last_scale is not None and scale >= last_scale:
                return None
            total += (current or 1) * scale
            current, last_scale, last = 0, scale, None
        else:
            return None
        seen = True
    return total + current if seen else None


def _one_phrase(tokens: list[str]) -> bool:
    """False when number words sit between two currency words ("... Dirhams
    One Thousand ... Dirhams"): that is two amounts, not one."""
    major = [i for i, t in enumerate(tokens) if t in _MAJOR]
    if len(major) < 2:
        return True
    numeric = {i for i, t in enumerate(tokens) if t in _UNITS or t in _TENS or t in _SCALES or t == "hundred"}
    return not any(major[0] < i < major[-1] for i in numeric)


def parse_english_amount(text: str | None) -> float | None:
    """The amount `text` spells in English words, or None if it can't be read
    with certainty."""
    if not text or re.search(r"\d", text):
        return None
    tokens = [t for t in re.split(r"[\s\-,./()]+", text.casefold()) if t]
    if not tokens or not _one_phrase(tokens):
        return None
    # "... dirhams and fifty fils": the part after the last major-currency
    # word, if it ends in a minor unit, is the fraction.
    major_at = max((i for i, t in enumerate(tokens) if t in _MAJOR), default=None)
    minor = [t for t in tokens if t in _MINOR]
    whole_tokens, fraction_tokens = tokens, []
    if minor and major_at is not None:
        whole_tokens, fraction_tokens = tokens[:major_at], tokens[major_at + 1 :]
    number_words = [t for t in whole_tokens if t not in _MAJOR | _FILLER | _MINOR]
    whole = _integer(number_words)
    if whole is None:
        return None
    fraction = 0
    if fraction_tokens:
        fraction_words = [t for t in fraction_tokens if t not in _MINOR | _FILLER]
        fraction = _integer(fraction_words) if fraction_words else 0
        if fraction is None or fraction >= 1000:
            return None
    divisor = 1000 if any(t in {"fils", "baisa", "baiza"} for t in minor) and fraction >= 100 else 100
    return whole + fraction / divisor


# A printed figure: "29,800", "AED 31,290.00", "1,490/-", "$ 2 500.50".
_FIGURE_RE = re.compile(
    r"(?<![\w.])(?:[A-Za-z]{2,3}\.?\s*|[$€£]\s*)?(?P<number>\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?:/-|[A-Za-z]{2,3}\.?(?![a-z]))?(?![\w.])"
)
_LABEL_WORDS_RE = re.compile(r"\b(?:in|amount\s+in)\s+words?\b|\bsay\b|\bonly\b|\bfigure", re.IGNORECASE)


def parse_figure(text: str | None) -> float | None:
    """The number a printed figure like "29,800" or "AED 1,490.00" shows, or
    None when `text` is not just one figure (with at most a currency mark)."""
    if not text:
        return None
    match = _FIGURE_RE.fullmatch(text.strip().strip("()[]:;"))
    if not match:
        return None
    try:
        return float(re.sub(r"[,\s]", "", match.group("number")))
    except ValueError:
        return None


def _looks_like_an_amount(words_text: str) -> bool:
    """Guards against reading any stray English word ("One", "Ten") as an
    amount in words: the phrase has a scale/hundred, or a currency or "only"."""
    tokens = set(re.split(r"[\s\-,./()]+", words_text.casefold()))
    return bool(tokens & (set(_SCALES) | {"hundred"})) or bool(tokens & (_MAJOR | _MINOR | {"only"}))


def find_amount_words(text: str | None) -> dict | None:
    """An amount written in English words inside one printed line or table
    cell, with whatever else that text carries:

        "Total Amount in Words (AED): Twenty-Nine Thousand Six Hundred Dirhams"
            -> label "Total Amount in Words (AED)", text "Twenty-Nine ...", value 29600
        "Total: AED 20,500 (Twenty Thousand Five Hundred Only)"
            -> label "Total", figure 20500, text "Twenty Thousand ...", value 20500
        "Twenty Thousand Only" -> label None, text as given, value 20000

    Returns {"label", "text", "value", "figure", "figure_raw_text"} or None
    when the text has no readable amount in words. Multi-line text is read
    line by line (a label line above the words line) and as a whole."""
    if not text:
        return None
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) > 1:
        # "Total Amount in Words (AED):" above "Twenty-Nine Thousand ..." in one cell.
        for index, line in enumerate(lines):
            found = find_amount_words(line)
            if found is None:
                continue
            if found["label"] is None and index > 0 and is_words_label(lines[index - 1]):
                found["label"] = lines[index - 1].rstrip(":").strip()
            return found
        return None
    line = lines[0]
    label: str | None = None
    body = line
    head, colon, tail = line.partition(":")
    if colon and head.strip() and parse_english_amount(_without_figures(tail)) is not None:
        label, body = head.strip(), tail.strip()
    figure_match = None
    words_text = body
    if re.search(r"\d", body):
        figures = list(_FIGURE_RE.finditer(body))
        if len(figures) != 1:
            return None
        figure_match = figures[0]
        words_text = _without_figures(body)
    words_text = words_text.strip(" ()[]-:;,.")
    value = parse_english_amount(words_text)
    if value is None or not _looks_like_an_amount(words_text):
        return None
    return {
        "label": label,
        "text": words_text,
        "value": value,
        "figure": float(re.sub(r"[,\s]", "", figure_match.group("number"))) if figure_match else None,
        "figure_raw_text": figure_match.group(0).strip() if figure_match else None,
    }


def _without_figures(text: str) -> str:
    return _FIGURE_RE.sub(" ", text)


def is_words_label(text: str | None, *, strict: bool = False) -> bool:
    """A line that introduces an amount in words ("Total Amount in Words
    (AED):", "Say:") rather than being one. `strict` requires the line to
    say so ("in words", "say", "only") — a bare "Something:" is not enough."""
    stripped = (text or "").strip()
    if not stripped or re.search(r"\d", stripped):
        return False
    named = bool(_LABEL_WORDS_RE.search(stripped))
    return (named or (not strict and stripped.endswith(":"))) and parse_english_amount(stripped) is None


def reads_as_amount_words(text: str | None) -> bool:
    """Every token is a number, currency or filler word — what a wrapped
    continuation of an amount in words ("Dirhams Only", "and Fifty Fils")
    looks like."""
    tokens = [t for t in re.split(r"[\s\-,./()]+", (text or "").casefold()) if t]
    known = set(_UNITS) | set(_TENS) | set(_SCALES) | {"hundred"} | _MAJOR | _MINOR | _FILLER
    return bool(tokens) and all(t in known for t in tokens)


_KINDS: tuple[tuple[str, re.Pattern], ...] = (
    ("grand_total", re.compile(r"\bgrand\b|\bnet\s+(?:payable|amount\s+due|total)\b|\bamount\s+due\b|\bbalance\s+due\b|\bpayable\b", re.I)),
    ("tax", re.compile(r"\bvat\b|\btax\b|\bgst\b", re.I)),
    ("subtotal", re.compile(r"\bsub[\s-]?total\b|\bnet\b|\bbefore\s+(?:vat|tax)\b|\bexcl", re.I)),
    ("total", re.compile(r"\btotal\b|\bamount\b|\bsum\b", re.I)),
    ("discount", re.compile(r"\bdiscount\b", re.I)),
    ("paid", re.compile(r"\bpaid\b|\badvance\b|\bdeposit\b", re.I)),
)


def amount_kind(label: str | None) -> str | None:
    """Which figure a label names: "grand_total", "tax", "subtotal", "total",
    "discount", "paid", or None when the label says nothing recognisable.
    "Total Amount in Words (AED)" is "total"; "VAT Amount in Words" is "tax";
    "Grand Total Amount in Words" is "grand_total"."""
    if not label:
        return None
    text = re.sub(r"\(?\b(?:amount\s+)?in\s+words?\b\)?|\([A-Za-z]{3}\)|\b(?:AED|USD|SAR|QAR|OMR|KWD|BHD|EUR|GBP|INR)\b", " ", label, flags=re.I)
    for kind, pattern in _KINDS:
        if pattern.search(text):
            return kind
    return None
