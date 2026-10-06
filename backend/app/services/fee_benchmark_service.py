"""
Fee benchmark check (issuer_verification sub-check `fee_benchmark`, scored by
the `issuer.fee_benchmark_deviation` rule): the charges on a document from a
KNOWN issuer, compared with that issuer's published fees
(`issuer_fee_benchmarks`, app/models/issuer_fee_benchmark.py).

Only runs when the issuer matched the registry AND benchmark rows exist for
it (and the document's grade, when the benchmarks are per grade); otherwise
"not_checked", with the reason.

Each line item is mapped to a charge key (tuition, books, uniform,
transport, technology, registration) by its label and compared with the
benchmark for that key from the nearest academic year within one year of the
document's, adjusted by +5% per year of difference (fees rise yearly). An
item more than 25% above its benchmark is flagged. When OTHER items match
their benchmarks within 2%, the reason says so: one inflated charge among
otherwise exact ones is the pattern of an altered figure, not of a different
fee schedule.

A benchmark "annual_total" (an all-in yearly fee) is compared with the
document's total, and — when the amount in words differs from the figures —
with the words too: words that fit the published fee while the figures
don't point at the figures having been changed.

Benchmarks from third-party listings (verified = false) are labelled as such
in every finding.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.issuer_fee_benchmark import IssuerFeeBenchmark
from app.models.issuer_registry import IssuerRegistry
from app.services.amount_words import parse_english_amount

# More than this far above the benchmark is flagged.
DEVIATION_LIMIT = 0.25
# Items within this of their benchmark count as "matching".
MATCH_TOLERANCE = 0.02
# Benchmarks this many academic years from the document's are usable.
MAX_YEAR_DISTANCE = 1
YEARLY_INCREASE = 0.05

_ITEM_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("tuition", re.compile(r"tuition|tution|school\s+fee|term\s*\d|fee\s+for\s+term|academic\s+fee", re.I)),
    ("books", re.compile(r"\bbooks?\b|text\s*books?", re.I)),
    ("uniform", re.compile(r"uniform", re.I)),
    ("transport", re.compile(r"transport|\bbus\b", re.I)),
    ("technology", re.compile(r"technolog|\btech\b|\bipad\b|laptop|\bit\s+fee", re.I)),
    ("registration", re.compile(r"registration|admission|enrol", re.I)),
]
_GRADE_FIELD_RE = re.compile(r"grade|class|year_group|level", re.I)
_YEAR_FIELD_RE = re.compile(r"academic_year|school_year", re.I)
_ACADEMIC_YEAR_RE = re.compile(r"(20\d{2})\s*[-/–]\s*(?:20)?(\d{2})\b")


def item_key(description: str | None) -> str | None:
    for key, pattern in _ITEM_KEYWORDS:
        if pattern.search(description or ""):
            return key
    return None


def normalize_grade(value: str | None) -> str | None:
    """"KG 2" / "kg-2" -> "KG2"; "Grade 5" / "G5" -> "GRADE 5"; "Year 3" -> "YEAR 3"."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value).strip().upper())
    if m := re.fullmatch(r"(KG|FS|PRE-?KG)\s*-?\s*(\d+)", text):
        return f"{m.group(1).replace('-', '')}{m.group(2)}"
    if m := re.fullmatch(r"(?:GRADE|GR\.?|G)\s*-?\s*(\d+)", text):
        return f"GRADE {m.group(1)}"
    if m := re.fullmatch(r"(?:YEAR|YR\.?|Y)\s*-?\s*(\d+)", text):
        return f"YEAR {m.group(1)}"
    return text


def _first_year(academic_year: str | None) -> int | None:
    match = re.match(r"\s*(20\d{2})", academic_year or "")
    return int(match.group(1)) if match else None


def _document_year(fields: dict[str, Any], ocr_text: str | None) -> int | None:
    """First calendar year of the academic year the document bills: an
    "academic_year" field, else a "2023-24"/"2022-2023" in the text, else
    from the document date (September onward starts a new year)."""
    for field in fields.get("additional_fields") or []:
        if isinstance(field, dict) and _YEAR_FIELD_RE.search(field.get("field_name") or ""):
            year = _first_year(str(field.get("value") or ""))
            if year:
                return year
    for match in _ACADEMIC_YEAR_RE.finditer(ocr_text or ""):
        if (int(match.group(2)) - int(match.group(1)[2:])) % 100 == 1:  # 2023-24, not 2023-07
            return int(match.group(1))
    raw = ((fields.get("core_fields") or {}).get("date") or {}).get("value")
    try:
        d = date.fromisoformat(raw) if raw else None
    except ValueError:
        d = None
    return None if d is None else (d.year if d.month >= 8 else d.year - 1)


def _grade(fields: dict[str, Any]) -> str | None:
    for field in fields.get("additional_fields") or []:
        if isinstance(field, dict) and _GRADE_FIELD_RE.search(field.get("field_name") or ""):
            return normalize_grade(field.get("value"))
    return None


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _source(row: IssuerFeeBenchmark) -> str:
    where = row.source_note or row.source_url or "source not recorded"
    return f"{where}; {'verified' if row.verified else 'third-party listing, not verified'}"


def _pick(rows: list[IssuerFeeBenchmark], key: str, year: int | None) -> tuple[IssuerFeeBenchmark, float] | None:
    """The benchmark row for `key` nearest the document's year (within
    MAX_YEAR_DISTANCE; undated rows "*" are used as they are) and its amount
    adjusted to that year."""
    candidates = []
    for row in rows:
        if row.item != key:
            continue
        row_year = _first_year(row.academic_year)
        if row_year is None:
            candidates.append((99, row, row.amount))
        elif year is not None and abs(year - row_year) <= MAX_YEAR_DISTANCE:
            candidates.append((abs(year - row_year), row, row.amount * (1 + YEARLY_INCREASE) ** (year - row_year)))
    if not candidates:
        return None
    _, row, amount = min(candidates, key=lambda c: c[0])
    return row, amount


def check_fee_benchmark(
    db: Session,
    company_id,
    issuer: IssuerRegistry | None,
    extracted_fields: dict[str, Any],
    ocr_text: str | None = None,
) -> dict[str, Any]:
    if issuer is None:
        return {"status": "not_checked", "reason": "Not checked: the issuer did not match the registry."}
    rows = list(
        db.execute(
            select(IssuerFeeBenchmark).where(
                IssuerFeeBenchmark.company_id == company_id, IssuerFeeBenchmark.issuer_id == issuer.id
            )
        ).scalars()
    )
    grade = _grade(extracted_fields)
    rows = [r for r in rows if r.grade == "*" or (grade is not None and normalize_grade(r.grade) == grade)]
    if not rows:
        return {
            "status": "not_checked",
            "reason": f"Not checked: no published fees on file for {issuer.name}"
                      + (f" ({grade})" if grade else "") + ".",
        }
    year = _document_year(extracted_fields, ocr_text)
    currency = ((extracted_fields.get("core_fields") or {}).get("amount") or {}).get("currency")
    rows = [r for r in rows if not currency or r.currency.upper() == str(currency).upper()]

    comparisons: list[dict[str, Any]] = []
    for item in extracted_fields.get("line_items") or []:
        if not isinstance(item, dict) or item.get("status") == "paid" or not item.get("counts_toward_total", True):
            continue
        key, amount = item_key(item.get("description")), item.get("line_total")
        if key is None or not isinstance(amount, (int, float)):
            continue
        picked = _pick(rows, key, year)
        if picked is None:
            continue
        row, expected = picked
        comparisons.append({
            "item": item.get("description"), "key": key, "amount": float(amount), "benchmark": round(expected, 2),
            "benchmark_year": row.academic_year, "source": _source(row),
            "deviation": round(amount / expected - 1, 4) if expected else None,
        })
    # Several lines of one key (term 1, 2, 3) are compared as their sum.
    by_key: dict[str, list[dict[str, Any]]] = {}
    for c in comparisons:
        by_key.setdefault(c["key"], []).append(c)
    for key, group in by_key.items():
        if len(group) > 1:
            total = sum(c["amount"] for c in group)
            merged = dict(group[0], item=" + ".join(c["item"] for c in group), amount=total,
                          deviation=round(total / group[0]["benchmark"] - 1, 4))
            comparisons = [c for c in comparisons if c["key"] != key] + [merged]

    annual = _pick(rows, "annual_total", year)
    total = ((extracted_fields.get("core_fields") or {}).get("amount") or {}).get("value")
    words_note = ""
    if annual is not None and isinstance(total, (int, float)):
        row, expected = annual
        comparisons.append({
            "item": "Total", "key": "annual_total", "amount": float(total), "benchmark": round(expected, 2),
            "benchmark_year": row.academic_year, "source": _source(row),
            "deviation": round(total / expected - 1, 4) if expected else None,
        })
        words = extracted_fields.get("amount_in_words") or {}
        in_words = parse_english_amount(words.get("text") or "") if words.get("text") else None
        in_words = in_words if in_words is not None else words.get("value")
        if isinstance(in_words, (int, float)) and abs(in_words - total) > 0.5 and expected:
            gap = in_words / expected - 1
            fits = abs(gap) <= DEVIATION_LIMIT
            words_note = (
                f" The amount in words ({_money(in_words)}) is {abs(gap):.0%} "
                f"{'above' if gap >= 0 else 'below'} the published fee — "
                + ("the words fit the published fee while the figures do not." if fits else "it does not fit either.")
            )

    if not comparisons:
        return {
            "status": "not_checked",
            "reason": f"Not checked: none of the charges has a published fee on file for {issuer.name}"
                      + (f" ({grade})" if grade else "") + " within a year of the document.",
        }
    over = [c for c in comparisons if c["deviation"] is not None and c["deviation"] > DEVIATION_LIMIT]
    matching = [c for c in comparisons if c["deviation"] is not None and abs(c["deviation"]) <= MATCH_TOLERANCE]
    scope = f"{issuer.name}" + (f" {grade}" if grade else "")
    if over:
        parts = [
            f"{c['item']} {_money(c['amount'])} is {c['deviation']:.0%} above the published "
            f"{_money(c['benchmark'])} ({scope}, {c['benchmark_year']}; {c['source']})"
            for c in over
        ]
        reason = "Charges far above the issuer's published fees: " + "; ".join(parts) + "."
        others = [c for c in matching if c not in over]
        if others:
            reason += (
                " The other charges match their published fees within 2% ("
                + ", ".join(f"{c['item']} {_money(c['amount'])}" for c in others)
                + ") — one inflated item among exact ones is typical of an altered figure."
            )
        return {"status": "flag", "reason": reason + words_note, "comparisons": comparisons}
    return {
        "status": "pass",
        "reason": f"All {len(comparisons)} compared charge(s) are within 25% of {scope}'s published fees." + words_note,
        "comparisons": comparisons,
    }
