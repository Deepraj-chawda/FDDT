"""
Reads an amount written out in English words ("Twenty Thousand Five Hundred
AED Only", "one hundred and five dollars and fifty cents") as a number.

Used by the amount-in-words check (app/services/field_validation_service.py)
so that comparison does not rest on the extraction model's own reading of the
words: a model asked to convert words that contradict the figures beside them
tends to "fix" them. Anything this cannot read with certainty (other
languages, digits mixed in, unknown words) returns None, and the caller falls
back to the extraction model's value.
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
    total, current, seen = 0, 0, False
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def parse_english_amount(text: str | None) -> float | None:
    """The amount `text` spells in English words, or None if it can't be read
    with certainty."""
    if not text or re.search(r"\d", text):
        return None
    tokens = [t for t in re.split(r"[\s\-,./()]+", text.casefold()) if t]
    if not tokens:
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
