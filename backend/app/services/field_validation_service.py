"""
Date/amount/consistency validation (SPECIFICATION.md section 3.1,
"Date/amount/consistency validation") — pure rule-based checks over one
document's own `extracted_fields` JSON (see app/services/llm_service.py's
DocumentAnalysis for that shape). No I/O, no DB access: app/tasks/
document_checks.py wraps this and writes the result to `document_checks`
(check_type "field_validation").

Amounts and dates are normalized at extraction time now (app/services/
llm_service.py — `amount`/`subtotal`/`tax_amount` are plain floats,
`date` is ISO 8601), specifically so this module doesn't need to
re-parse raw text in whatever format/script/numeral-system a document
happened to use. Read `.value` fields directly; never read `.raw_text`
for logic, only for display.

Independent sub-checks, each producing its own {status, reason} in the
returned `details` dict:
  - date_in_future: the document's own `date` core field is after today.
  - line_item_arithmetic: for each extracted line item, quantity x unit
    price (less any line discount) equals the line total printed.
  - subtotal_line_item_consistency: the line items that count toward the
    total add up to the subtotal — or, with no subtotal shown, to the
    total (less tax when tax is shown). Rows that are themselves totals
    ("Total Fees", "Grand total", "Subtotal") are left out, and so are
    installment rows ("Term I/II/III", "Installment 1/2", "Payment 1")
    that split another line into parts (their sum equals that line).
  - installment_consistency: installment rows add up to the line they split
    ("Term I 13,480 + Term II 10,266 + Term III 10,266 = Tuition Fees
    34,012"); flagged when they come close to one line but do not add up. Uses the structured `line_items`
    extraction; documents extracted before it existed fall back to
    additional_fields whose name suggests a line-item total, against the
    subtotal only.
  - total_tax_consistency: the `amount` core field (the grand total) vs.
    `subtotal + tax_amount` when a tax amount is shown separately, or
    vs. `subtotal` alone when it isn't (a document with no visible tax
    line should validate total == subtotal, not be flagged for
    "missing" tax). Skipped when there's no subtotal to check against.
  - tax_rate_consistency: the tax amount equals the taxable base x the
    stated tax rate (the extracted rate, else the printed label "VAT (5%)").
    A rate above 0 with no tax at all on a non-zero base is flagged: a
    zero-rated fee would not be labelled 5%. Otherwise the tax is the rate
    x — the whole subtotal, or (mixed-rate invoices, where only
    some charges are taxed) the sum of some subset of the payable line items;
    the matching subset is named. All subsets are tried up to 20 lines.
  - (subtotal_line_item_consistency, multi-column tables) when the line items
    were read from a table with separate amount columns (Price / Discount /
    Net / VAT / Total), each column is summed on its own and compared with
    its own total row — Net with "Total Net Price", VAT with "VAT", Total
    with "Total Price" — never one row's Net with another's Total.
  - amount_in_words_consistency: the amount written out in words equals
    the total (or the subtotal). English words are read here
    (app/services/amount_words.py), not taken from the extraction model,
    which tends to "correct" words that contradict the figures.
  - reference_number_format: the `reference_number` core field looks
    like a plausible reference/invoice number (contains a digit, no
    stray/garbled characters) rather than obviously-malformed
    extraction output.
  - iban_trn_validation: every printed IBAN has its country's length and a
    valid mod-97 checksum; a UAE IBAN's bank code matches the printed bank
    and it contains the printed account number; a UAE TRN is 15 digits
    starting with 100 (app/services/payment_identifiers.py).
  - rescan_conflict: a phone-scanner watermark ("Scanned with CamScanner")
    on a PDF produced by an office MFP — printed and re-scanned, so edits
    made before printing are invisible to pixel forensics.
  - period_quantity_consistency: a line listing the months it bills as
    due charges for that many months.
  - document_date_vs_file_creation: the date printed on the document is
    more than a day after the PDF file's own CreationDate, and the file was
    modified on or after that printed date — an older file edited and
    re-dated. A file generated in advance and never touched again (dated for
    a term start, say) passes.
  - date_sequence: the dates on and around the document are in order —
    the document's date, then the date a browser printed the page (its
    print header, "8/18/23, 12:54 PM"), then the PDF file's creation. A page
    printed before its own date, or a PDF made before the page was printed,
    is flagged. Without a print date there is nothing to add to
    document_date_vs_file_creation, and it is skipped.
  - fake_scan_watermark: a scanner app's line ("Scanned with CamScanner",
    "Adobe Scan") on a file with no raster image at all — nothing was
    scanned; the line was typed in to look authentic.
  - stamp_authenticity: a detected stamp that is live text and/or vector
    lines in the file (app/services/forensics/pdf_facts.py region_material),
    not an image of an ink stamp — a stamp typed into the document.
    synthetic_stamp_unsigned adds that no signature was found either.
  - tax_invoice_trn: a document titled TAX INVOICE carries no UAE TRN (15
    digits, starting 100) — a UAE tax invoice must show the supplier's TRN.
  - multiple_invoices / per_invoice_checks: a file holding several
    invoices (one per page, found and extracted page by page —
    app/services/multi_invoice.py) — two of them for the same month is
    flagged, and each invoice's own arithmetic is checked.
  - stamp_issuer_consistency: the organisation named on the document's stamp
    (its text as read by signature/stamp detection) is the issuer. Compared
    per writing system, with addresses, P.O. boxes, branch numbers and UAE
    place names left out, common abbreviations expanded (MBZ = Mohamed Bin
    Zayed, Intl = International) and generic words dropped (School, Campus,
    LLC, Branch, Private); token-set similarity of 80 or more is a match.
    Skipped when no stamp text was read or the stamp and the issuer share no
    script.

Arithmetic is compared to the cent (one cent of rounding per term), not
by a percentage: a 1% allowance on a 55,500 total would hide a 400
discrepancy, which is exactly the kind of edit these checks exist for.
Every number is the one PRINTED on the document — extraction is told
never to recompute them.

A sub-check that doesn't apply (field missing, nothing to compare) is
reported as "skipped" rather than "pass" or "flag" — it isn't evidence of
anything either way. The overall `result` is "flag" if any sub-check
flags, else "pass".
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from app.services.amount_words import parse_english_amount
from app.services.field_regions import format_field_value, make_located_region, make_region
from app.services.payment_identifiers import normalize as normalize_identifier
from app.services.payment_identifiers import validate_iban, validate_uae_trn

# A reference/invoice number should have at least one digit and consist
# only of alphanumerics plus common separators — a weak but effective
# guard against garbled/empty extraction output, not a strict per-issuer
# format (there is no single format across issuers/countries).
_REFERENCE_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-/_. #]*[A-Za-z0-9])?$")

# additional_fields whose name suggests a per-line-item total, e.g.
# "line_item_1_total", "item2_total" (both contain "item") — see the
# module docstring's "best-effort only" note. Deliberately excludes
# "price"/"cost": those usually name a per-unit price (e.g.
# "item1_unit_price"), which would double-count alongside that same
# line's own "item1_total".
_LINE_ITEM_NAME_HINTS = ("item",)
_LINE_ITEM_VALUE_HINTS = ("total", "amount")

# A handful of ISO-adjacent/legacy formats tried after ISO 8601 — belt
# and suspenders in case a value predates the extraction-time
# normalization fix, or an LLM response is slightly malformed. New
# extractions should always produce ISO 8601 directly (see
# app/services/llm_service.py); this is a safety net, not the primary path.
_FALLBACK_DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%B %d, %Y", "%d %B %Y", "%B %d %Y")


def _rounding_allowance(terms: int = 1) -> float:
    """How far two printed amounts may differ and still agree: one cent per
    rounded term that went into them."""
    return 0.01 * max(terms, 1) + 1e-9


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _qty(value: float) -> str:
    return f"{value:g}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _flag(reason: str, *regions: dict[str, Any] | None) -> dict[str, Any]:
    """A flagged sub-check result. `regions` are the highlight regions of the
    field(s) involved (see app/services/field_regions.py); fields with no
    stored location contribute nothing, and the key is omitted if none did."""
    result: dict[str, Any] = {"status": "flag", "reason": reason}
    drawn = [r for r in regions if r]
    if drawn:
        result["regions"] = drawn
    return result


def _check_date_in_future(core_fields: dict[str, Any]) -> dict[str, Any]:
    raw = (core_fields.get("date") or {}).get("value")
    if raw is None:
        return {"status": "skipped", "reason": "No date extracted."}
    parsed = _parse_date(raw)
    if parsed is None:
        return {
            "status": "skipped",
            "reason": f"Could not parse date {raw!r}.",
        }
    if parsed > date.today():
        return _flag(
            f"Document date {parsed.isoformat()} is in the future.",
            make_region(core_fields, "date", f"Field exception: Date ({raw} is in the future)"),
        )
    return {
        "status": "pass",
        "reason": f"Document date {parsed.isoformat()} is not in the future.",
    }


def _line_item_amounts(additional_fields: list[dict[str, Any]]) -> list[tuple[str, float]]:
    amounts: list[tuple[str, float]] = []
    for field in additional_fields:
        name = (field.get("field_name") or "").lower()
        if not any(hint in name for hint in _LINE_ITEM_NAME_HINTS):
            continue
        if not any(hint in name for hint in _LINE_ITEM_VALUE_HINTS):
            continue
        value = field.get("value")
        try:
            amount = float(str(value).replace(",", "")) if value is not None else None
        except ValueError:
            amount = None
        if amount is not None:
            amounts.append((field["field_name"], amount))
    return amounts


def _line_label(index: int, item: dict[str, Any]) -> str:
    description = (item.get("description") or "").strip()
    if len(description) > 40:
        description = description[:39].rstrip() + "…"
    return f"line {index}" + (f" ({description})" if description else "")


def _check_line_item_arithmetic(line_items: list[dict[str, Any]]) -> dict[str, Any]:
    checked = 0
    wrong: list[str] = []
    regions: list[dict[str, Any] | None] = []
    for index, item in enumerate(line_items, start=1):
        quantity, unit_price, total = _num(item.get("quantity")), _num(item.get("unit_price")), _num(
            item.get("line_total")
        )
        if quantity is None or unit_price is None or total is None:
            continue
        checked += 1
        discount = _num(item.get("discount")) or 0.0
        expected = quantity * unit_price - discount
        # A unit price rounded to the cent, times the quantity, can drift by
        # half a cent per unit.
        allowance = _rounding_allowance(2) + abs(quantity) * 0.005
        if abs(expected - total) <= allowance:
            continue
        less = f" − discount {_money(discount)}" if discount else ""
        calc = f"{_qty(quantity)} × {_money(unit_price)}{less} = {_money(expected)}"
        wrong.append(f"{_line_label(index, item)}: {calc}, but the line total printed is {_money(total)}")
        regions.append(
            make_located_region(
                item, f"line_item_{index}", f"Line {index} total", _money(total),
                f"Field exception: Line {index} total ({_money(total)} — {calc})",
            )
        )
    if not checked:
        return {"status": "skipped", "reason": "No line items with a quantity, unit price and total to check."}
    if wrong:
        return _flag(f"{len(wrong)} line item(s) do not add up: " + "; ".join(wrong) + ".", *regions)
    return {
        "status": "pass",
        "reason": f"All {checked} line item(s): quantity × unit price matches the line total.",
    }


def _sum_target(core_fields: dict[str, Any]) -> tuple[str, float, str] | None:
    """What the line items should add up to: (core field, value, label)."""
    subtotal = _num((core_fields.get("subtotal") or {}).get("value"))
    if subtotal is not None:
        return "subtotal", subtotal, f"subtotal {_money(subtotal)}"
    total = _num((core_fields.get("amount") or {}).get("value"))
    if total is None:
        return None
    tax = _num((core_fields.get("tax_amount") or {}).get("value"))
    if tax is not None:
        return "amount", total - tax, f"total {_money(total)} less tax {_money(tax)} ({_money(total - tax)})"
    return "amount", total, f"total {_money(total)}"


def _line_names(lines: list[tuple[int, dict[str, Any], float]]) -> list[str]:
    return [f"{_line_label(i, item)}: {_money(total)}" for i, item, total in lines]


# A row that is itself a total, not a charge.
_TOTAL_ROW_RE = re.compile(
    r"^\W*(?:grand\s+|sub[\s-]?|net\s+)?total\b|\btotal\s+(?:fees?|amount|due|payable|charges?)\b"
    r"|^\W*(?:amount|balance)\s+due\b|^\W*net\s+payable\b",
    re.IGNORECASE,
)
# A row that is one part of a payment schedule.
_INSTALLMENT_RE = re.compile(
    r"\b(?:term|installment|instalment|payment|semester|quarter)\s*(?:no\.?\s*)?"
    r"(?:[ivxl]{1,4}|\d{1,2}|one|two|three|four)\b"
    r"|\b(?:1st|2nd|3rd|4th|first|second|third|fourth)\s+(?:term|installment|instalment|payment|semester)\b",
    re.IGNORECASE,
)
# Installments within this share of a line (but not equal to it) are a
# broken split of that line.
_NEAR_PARENT_SHARE = 0.2

Row = tuple[int, dict[str, Any], float]


def _description(item: dict[str, Any]) -> str:
    return str(item.get("description") or "")


def _installment_split(rows: list[Row]) -> tuple[list[Row], Row | None, bool]:
    """(installment rows, the line they split, whether they add up to it).
    The installments are the rows labelled as parts of a payment schedule,
    when there are two or more; the line they split is the other row their
    sum equals — or, failing that, the largest other row within
    _NEAR_PARENT_SHARE of it (a split that no longer adds up). No line:
    the installments are charges in their own right."""
    parts = [r for r in rows if _INSTALLMENT_RE.search(_description(r[1]))]
    if len(parts) < 2:
        return [], None, False
    others = [r for r in rows if r not in parts]
    total = sum(r[2] for r in parts)
    exact = next((r for r in others if abs(r[2] - total) <= _rounding_allowance(len(parts))), None)
    if exact is not None:
        return parts, exact, True
    largest = max(others, key=lambda r: r[2], default=None)
    if (
        largest is not None and largest[2] >= max(r[2] for r in parts)
        and abs(largest[2] - total) <= _NEAR_PARENT_SHARE * largest[2]
    ):
        return parts, largest, False
    return [], None, False


def _payable_rows(line_items: list[dict[str, Any]]) -> tuple[list[Row], list[tuple[Row, str]]]:
    """(priced rows that are charges, [(row, why it is left out)]): total
    rows and the installments of another line are not charges."""
    priced = [(i, item, _num(item.get("line_total"))) for i, item in enumerate(line_items, start=1)]
    priced = [(i, item, total) for i, item, total in priced if total is not None]
    totals = [r for r in priced if _TOTAL_ROW_RE.search(_description(r[1]))]
    rest = [r for r in priced if r not in totals]
    parts, parent, _ = _installment_split(rest)
    left_out = [(r, "a total row") for r in totals]
    if parent is not None:
        name = _description(parent[1]) or f"line {parent[0]}"
        left_out += [(r, f"an installment of {name}") for r in parts]
    return [r for r in rest if not (parent is not None and r in parts)], left_out


def _check_installment_consistency(line_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Installment rows add up to the line they split (module docstring)."""
    priced = [(i, item, _num(item.get("line_total"))) for i, item in enumerate(line_items, start=1)]
    priced = [(i, item, total) for i, item, total in priced if total is not None]
    rest = [r for r in priced if not _TOTAL_ROW_RE.search(_description(r[1]))]
    parts, parent, adds_up = _installment_split(rest)
    if parent is None:
        return {"status": "skipped", "reason": "No installment rows splitting another line."}
    terms = " + ".join(f"{_description(r[1]) or f'line {r[0]}'} {_money(r[2])}" for r in parts)
    split = sum(r[2] for r in parts)
    name = _description(parent[1]) or f"line {parent[0]}"
    if adds_up:
        return {"status": "pass", "reason": f"{terms} = {_money(split)}, the {name} line."}
    return _flag(
        f"The installments do not add up to the line they split: {terms} = {_money(split)}, but {name} is "
        f"{_money(parent[2])} (difference {_money(abs(parent[2] - split))})."
    )


def _check_line_items_sum(core_fields: dict[str, Any], line_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The structured line-item version of subtotal_line_item_consistency;
    None when there are no structured line totals to use. Lines marked paid
    (status "paid", read from the document's own PAID marker — see
    app/services/line_item_parsing.py) are listed but never summed. The
    result names the lines that were summed and the ones left out."""
    priced, set_aside = _payable_rows(line_items)
    counted = [(i, item, total) for i, item, total in priced if item.get("counts_toward_total", True)]
    if not counted:
        return None
    target = _sum_target(core_fields)
    if target is None:
        return {"status": "skipped", "reason": "No subtotal or total to compare the line items against."}
    field, expected, label = target
    counted_sum = sum(total for _, _, total in counted)
    excluded = [p for p in priced if p not in counted]
    detail = {
        "summed_lines": _line_names(counted),
        "excluded_lines": _line_names(excluded) + [f"{name} ({why})" for name, (_, why) in zip(
            _line_names([r for r, _ in set_aside]), set_aside
        )],
    }
    # Rounding only: one cent per summed line (a printed sum of rounded
    # amounts can only drift that far). A percentage would hide real edits —
    # 1% of 55,500 is 555.
    allowance = _rounding_allowance(len(counted))
    if abs(counted_sum - expected) <= allowance:
        return {"status": "pass", "reason": f"The {len(counted)} line item(s) add up to the {label}.", **detail}
    # The extraction's "counts toward total" call is a judgment: if every
    # priced line together matches instead, that is not an inconsistency —
    # unless a line is marked paid on the document itself.
    unpaid = [p for p in priced if p[1].get("status") != "paid"]
    all_sum = sum(total for _, _, total in unpaid)
    if len(unpaid) != len(counted) and abs(all_sum - expected) <= _rounding_allowance(len(unpaid)):
        return {
            "status": "pass",
            "reason": f"The {len(unpaid)} line item(s) add up to the {label}.",
            "summed_lines": _line_names(unpaid),
            "excluded_lines": _line_names([p for p in priced if p not in unpaid])
            + [f"{name} ({why})" for name, (_, why) in zip(_line_names([r for r, _ in set_aside]), set_aside)],
        }
    terms = " + ".join(_money(total) for _, _, total in counted)
    left_out_names = [
        f"{name}{' (marked paid)' if item.get('status') == 'paid' else ''}"
        for name, (_, item, _) in zip(_line_names(excluded), excluded)
    ] + [f"{name} ({why})" for name, (_, why) in zip(_line_names([r for r, _ in set_aside]), set_aside)]
    left_out = " Not summed: " + "; ".join(left_out_names) + "." if left_out_names else ""
    return {
        **_flag(
            f"Line items = {_money(counted_sum)}, stated {label}, difference "
            f"{_money(abs(expected - counted_sum))} (summed: {terms}).{left_out}",
            make_region(
                core_fields, field,
                f"Field exception: {'Subtotal' if field == 'subtotal' else 'Total amount'} "
                f"({format_field_value(field, core_fields.get(field))} vs line items {_money(counted_sum)})",
            ),
        ),
        **detail,
    }


# Each amount column and the core field its total row is, when the document
# has no labelled total row for it: net -> subtotal, VAT -> tax, total -> amount.
_COLUMN_CORE = {"net": "subtotal", "vat": "tax_amount", "total": "amount"}
_COLUMN_NAMES = {"net": "Net", "vat": "VAT", "total": "Total", "price": "Price", "discount": "Discount"}


def _check_column_sums(
    core_fields: dict[str, Any], line_items: list[dict[str, Any]], column_totals: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Multi-column line items (app/services/line_item_parsing.py
    parse_multi_column_tables): each amount column summed on its own and
    compared with its own total row — Net with "Total Net Price", VAT with
    "VAT", Total with "Total Price". Never mixed. None when the line items
    do not carry columns."""
    counted = [i for i in line_items if i.get("counts_toward_total", True) and i.get("status") != "paid"]
    if not counted or not all(isinstance(i.get("columns"), dict) for i in counted):
        return None
    headers = next((i.get("column_headers") for i in counted if i.get("column_headers")), {}) or {}
    compared, wrong = [], []
    anchored = False  # a net or total column was compared
    for role, core_name in _COLUMN_CORE.items():
        values = [i["columns"][role] for i in counted if role in i["columns"]]
        if not values:
            continue
        target = (column_totals or {}).get(role)
        expected = target["value"] if target else _num((core_fields.get(core_name) or {}).get("value"))
        label = target["label"] if target else core_name.replace("_", " ")
        has_total_column = any("total" in i["columns"] for i in counted)
        if expected is None and role == "net" and not has_total_column:
            # A "Net Amount" column with no total column beside it is what
            # each row comes to: it adds up to the document's total.
            expected, label = _num((core_fields.get("amount") or {}).get("value")), "total"
        if expected is None:
            continue
        if role in ("net", "total"):
            anchored = True
        column = headers.get(role) or _COLUMN_NAMES[role]
        total = sum(values)
        line = f"{column} column {_money(total)} vs {label} {_money(expected)}"
        if abs(total - expected) <= _rounding_allowance(len(values)):
            compared.append(f"{column} column {_money(total)} = {label}")
        else:
            wrong.append((role, core_name, f"{line} (difference {_money(abs(total - expected))})"))
    if not anchored:
        return None  # only the VAT column could be compared: the plain check says more
        return None
    detail = {"summed_lines": _line_names([(n, i, i["line_total"]) for n, i in enumerate(counted, 1)]),
              "excluded_lines": [], "columns_compared": compared + [w[2] for w in wrong]}
    if not wrong:
        return {"status": "pass", "reason": f"Each column adds up to its own total: {'; '.join(compared)}.", **detail}
    return {
        **_flag(
            "A column does not add up to its own total: " + "; ".join(w[2] for w in wrong) + "."
            + (f" Correct: {'; '.join(compared)}." if compared else ""),
            *[make_region(core_fields, core_name, f"Field exception: {text}") for _, core_name, text in wrong],
        ),
        **detail,
    }


def _check_subtotal_line_item_consistency(
    core_fields: dict[str, Any], additional_fields: list[dict[str, Any]],
    line_items: list[dict[str, Any]] | None = None, column_totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_column = _check_column_sums(core_fields, line_items or [], column_totals)
    if by_column is not None:
        return by_column
    structured = _check_line_items_sum(core_fields, line_items or [])
    if structured is not None:
        return structured

    # Documents extracted before structured line items existed.
    subtotal = (core_fields.get("subtotal") or {}).get("value")
    line_items = _line_item_amounts(additional_fields)

    if subtotal is None or not line_items:
        return {
            "status": "skipped",
            "reason": "No subtotal and/or line-item amount fields to compare.",
        }

    line_item_sum = sum(amount for _, amount in line_items)
    if abs(line_item_sum - subtotal) > _rounding_allowance(len(line_items)):
        return _flag(
            f"Subtotal {subtotal:.2f} does not match the sum of "
            f"{len(line_items)} line-item field(s) ({line_item_sum:.2f}).",
            make_region(
                core_fields, "subtotal",
                f"Field exception: Subtotal ({format_field_value('subtotal', core_fields.get('subtotal'))} "
                f"vs line items {line_item_sum:,.2f})",
            ),
        )
    return {
        "status": "pass",
        "reason": f"Subtotal matches the sum of {len(line_items)} line-item field(s).",
    }


def _check_total_tax_consistency(core_fields: dict[str, Any]) -> dict[str, Any]:
    total = (core_fields.get("amount") or {}).get("value")
    subtotal = (core_fields.get("subtotal") or {}).get("value")
    tax_amount = (core_fields.get("tax_amount") or {}).get("value")

    if total is None or subtotal is None:
        return {
            "status": "skipped",
            "reason": "No total and/or subtotal to compare.",
        }

    if tax_amount is not None:
        expected = subtotal + tax_amount
        tax_note = f" + tax {tax_amount:.2f}"
    else:
        # No tax line shown at all — the correct expectation is total ==
        # subtotal, not a flag for "missing" tax (a great many documents
        # genuinely have no tax).
        expected = subtotal
        tax_note = " (no tax shown)"

    if abs(expected - total) > _rounding_allowance(2):
        return _flag(
            f"Total {total:.2f} does not match subtotal {subtotal:.2f}{tax_note} "
            f"(expected {expected:.2f}).",
            make_region(
                core_fields, "amount",
                f"Field exception: Total amount ({format_field_value('amount', core_fields.get('amount'))} "
                f"vs expected {expected:,.2f})",
            ),
            make_region(core_fields, "subtotal", f"Field exception: Subtotal (expected total is {expected:,.2f})"),
            make_region(core_fields, "tax_amount", f"Field exception: Tax amount (expected total is {expected:,.2f})")
            if tax_amount is not None else None,
        )
    return {
        "status": "pass",
        "reason": f"Total matches subtotal {subtotal:.2f}{tax_note}.",
    }


# Above this many line items, trying every subset of them as the taxed base
# (2^n) is too slow; only the whole subtotal is tried.
_MAX_TAX_SUBSET_LINES = 20


def _taxed_subset(
    lines: list[tuple[int, dict[str, Any], float]], rate: float, tax: float
) -> list[tuple[int, dict[str, Any], float]] | None:
    """The smallest set of line items that `rate` % of gives `tax` — for
    mixed-rate invoices, where only some charges are taxed (UAE school
    invoices: tuition, books and transport zero-rated; uniform and
    technology fees at 5%). Tolerance: one cent per taxed line (rounding
    only)."""
    from itertools import combinations

    if not 0 < len(lines) <= _MAX_TAX_SUBSET_LINES or rate <= 0:
        return None
    for size in range(1, len(lines) + 1):
        allowance = _rounding_allowance(size)
        for subset in combinations(lines, size):
            if abs(sum(t for _, _, t in subset) * rate / 100 - tax) <= allowance:
                return list(subset)
    return None


# A printed tax label with its rate: "VAT (5%)", "VAT 5 %", "Tax @ 5%".
_TAX_LABEL_RE = re.compile(r"\b(?:VAT|TAX|GST)\b\s*[(@:]?\s*(\d{1,2}(?:\.\d+)?)\s*%", re.IGNORECASE)


def _label_rate(ocr_text: str | None) -> float | None:
    rates = {float(m[1]) for m in _TAX_LABEL_RE.finditer(ocr_text or "")}
    return rates.pop() if len(rates) == 1 else None


def _check_tax_rate_consistency(
    core_fields: dict[str, Any], line_items: list[dict[str, Any]] | None = None, ocr_text: str | None = None
) -> dict[str, Any]:
    rate = _num((core_fields.get("tax_rate") or {}).get("value"))
    rate_source = "rate"
    if rate is None:
        rate, rate_source = _label_rate(ocr_text), "label"
    tax = _num((core_fields.get("tax_amount") or {}).get("value"))
    if rate is None or tax is None:
        return {"status": "skipped", "reason": "No tax rate and/or tax amount to compare."}
    subtotal = _num((core_fields.get("subtotal") or {}).get("value"))
    total = _num((core_fields.get("amount") or {}).get("value"))
    if subtotal is not None:
        base, base_label = subtotal, f"subtotal {_money(subtotal)}"
    elif total is not None:
        base, base_label = total - tax, f"total less tax {_money(total - tax)}"
    else:
        return {"status": "skipped", "reason": "No subtotal or total to apply the tax rate to."}
    expected = base * rate / 100
    if abs(expected - tax) <= _rounding_allowance(2):
        return {"status": "pass", "reason": f"Tax {_money(tax)} is {rate:g}% of the {base_label}."}
    if rate > 0 and abs(tax) < 0.005 and base > 0:
        label = "printed label" if rate_source == "label" else "tax rate"
        return _flag(
            f"The {label} says {rate:g}% tax, but the tax is 0.00 on the {base_label} ({rate:g}% would be "
            f"{_money(expected)}). A zero-rated fee would not be labelled {rate:g}%.",
            make_region(core_fields, "tax_amount", f"Field exception: Tax amount (0.00 vs {rate:g}% = {_money(expected)})"),
        )
    payable = [
        (i, item, t)
        for i, item in enumerate(line_items or [], start=1)
        if (t := _num(item.get("line_total"))) is not None
        and item.get("counts_toward_total", True)
        and item.get("status") != "paid"
    ]
    subset = _taxed_subset(payable, rate, tax)
    if subset is not None:
        names = ", ".join((item.get("description") or f"line {i}").strip() for i, item, _ in subset)
        taxed = sum(t for _, _, t in subset)
        return {
            "status": "pass",
            "reason": f"Tax applies to {{{names}}} = {_money(taxed)}: {rate:g}% of it is {_money(tax)} "
                      f"(the other line items are not taxed).",
            "taxed_lines": [_line_label(i, item) for i, item, _ in subset],
        }
    no_subset = (
        " No combination of the line items explains it either."
        if 0 < len(payable) <= _MAX_TAX_SUBSET_LINES else ""
    )
    return _flag(
        f"Tax {_money(tax)} is not {rate:g}% of the {base_label} (expected {_money(expected)}).{no_subset}",
        make_region(
            core_fields, "tax_amount",
            f"Field exception: Tax amount ({format_field_value('tax_amount', core_fields.get('tax_amount'))} "
            f"vs {rate:g}% = {_money(expected)})",
        ),
        make_region(core_fields, "tax_rate", f"Field exception: Tax rate ({rate:g}% gives {_money(expected)})"),
    )


def _check_amount_in_words_consistency(
    core_fields: dict[str, Any], amount_in_words: dict[str, Any] | None
) -> dict[str, Any]:
    words = amount_in_words or {}
    text = (words.get("text") or "").strip()
    if not text:
        return {"status": "skipped", "reason": "No amount written in words."}
    value, read_by = parse_english_amount(text), "read from the words"
    if value is None:
        value, read_by = _num(words.get("value")), "as read by the extraction model"
    if value is None:
        return {"status": "skipped", "reason": f"Could not read the amount in words ({text!r})."}

    candidates = [
        (name, label, _num((core_fields.get(name) or {}).get("value")))
        for name, label in (("amount", "total"), ("subtotal", "subtotal"))
    ]
    candidates = [(name, label, v) for name, label, v in candidates if v is not None]
    if not candidates:
        return {"status": "skipped", "reason": "No total to compare the amount in words against."}

    def agrees(figure: float) -> bool:
        # Words often leave out the fils/cents ("... Five Hundred Only").
        return abs(value - figure) <= _rounding_allowance() or (value.is_integer() and int(figure) == value)

    for _, label, figure in candidates:
        if agrees(figure):
            return {"status": "pass", "reason": f"The amount in words ({_money(value)}) matches the {label}."}
    _, label, figure = candidates[0]
    return _flag(
        f"The amount in words, {text!r}, is {_money(value)} ({read_by}), but the {label} in figures is "
        f"{_money(figure)}.",
        make_region(
            core_fields, candidates[0][0],
            f"Field exception: {label.capitalize()} ({_money(figure)} vs {_money(value)} in words)",
        ),
        make_located_region(
            words, "amount_in_words", "Amount in words", text,
            f"Field exception: Amount in words ({_money(value)} vs {label} {_money(figure)})",
        ),
    )


def _check_reference_number_format(core_fields: dict[str, Any]) -> dict[str, Any]:
    raw = (core_fields.get("reference_number") or {}).get("value")
    if raw is None:
        return {"status": "skipped", "reason": "No reference number extracted."}
    text = raw.strip()
    if not text or not _REFERENCE_NUMBER_PATTERN.match(text) or not any(c.isdigit() for c in text):
        return _flag(
            f"Reference number {raw!r} doesn't look like a plausible reference/invoice number.",
            make_region(
                core_fields, "reference_number",
                f"Field exception: Reference number ({raw} has an unexpected format)",
            ),
        )
    return {"status": "pass", "reason": f"Reference number {raw!r} matches the expected format."}


# --- payment identifiers (IBAN / UAE TRN) ---------------------------------------

_IBAN_FIELD_RE = re.compile(r"iban", re.IGNORECASE)
_TRN_FIELD_RE = re.compile(r"(?:^|_)trn(?:$|_)|tax_registration|vat_(?:number|no|id|registration)", re.IGNORECASE)
_BANK_NAME_FIELD_RE = re.compile(r"bank_name|(?:^|_)bank$", re.IGNORECASE)
_ACCOUNT_FIELD_RE = re.compile(r"account_(?:number|no)", re.IGNORECASE)
# An IBAN printed after its label, for documents whose extraction has no IBAN field.
_IBAN_IN_TEXT_RE = re.compile(r"\bIBAN\b[^A-Z0-9]{0,30}([A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30})")


def _fields_named(
    additional_fields: list[dict[str, Any]], pattern: re.Pattern, exclude: re.Pattern | None = None
) -> list[dict[str, Any]]:
    return [
        f for f in additional_fields
        if isinstance(f, dict) and f.get("value") and pattern.search(f.get("field_name") or "")
        and not (exclude and exclude.search(f.get("field_name") or ""))
    ]


def _check_iban_trn_validation(
    core_fields: dict[str, Any], additional_fields: list[dict[str, Any]], ocr_text: str | None
) -> dict[str, Any]:
    """IBAN length/checksum/bank code/account number and UAE TRN format
    (app/services/payment_identifiers.py)."""
    found = [(f["value"], f) for f in _fields_named(additional_fields, _IBAN_FIELD_RE)]
    if not found and ocr_text:
        found = [(m.group(1), None) for m in _IBAN_IN_TEXT_RE.finditer(ocr_text)]
    ibans: list[tuple[str, dict[str, Any] | None]] = []
    seen: set[str] = set()
    for raw, field in found:
        if normalize_identifier(raw) not in seen:
            seen.add(normalize_identifier(raw))
            ibans.append((raw, field))
    bank = next((f["value"] for f in _fields_named(additional_fields, _BANK_NAME_FIELD_RE)), None)
    account = next(
        (f["value"] for f in _fields_named(additional_fields, _ACCOUNT_FIELD_RE, _IBAN_FIELD_RE)), None
    )
    currency = ((core_fields.get("amount") or {}).get("currency") or "").upper()
    uae = currency == "AED" or any(normalize_identifier(raw).startswith("AE") for raw, _ in ibans)
    trns = [(f["value"], f) for f in _fields_named(additional_fields, _TRN_FIELD_RE)] if uae else []

    checked: list[dict[str, Any]] = []
    failed: list[str] = []
    regions: list[dict[str, Any] | None] = []
    for raw, field in ibans:
        result = validate_iban(raw, bank_name=bank, account_number=account)
        checked.append({"kind": "IBAN", **result})
        if not result["ok"]:
            failed.append(f"IBAN {result['value']}: " + "; ".join(result["problems"]))
            if field is not None:
                regions.append(
                    make_located_region(
                        field, "iban", "IBAN", raw, f"Field exception: IBAN ({'; '.join(result['problems'])})"
                    )
                )
    for raw, field in trns:
        result = validate_uae_trn(raw)
        checked.append({"kind": "TRN", **result})
        if not result["ok"]:
            failed.append(f"TRN {raw}: " + "; ".join(result["problems"]))
            regions.append(
                make_located_region(field, "trn", "TRN", raw, f"Field exception: TRN ({'; '.join(result['problems'])})")
            )
    if not checked:
        return {"status": "skipped", "reason": "No IBAN or UAE TRN on the document."}
    if failed:
        return {**_flag("Invalid payment identifier(s): " + " | ".join(failed) + ".", *regions), "identifiers": checked}
    passed = "; ".join(f"{c['kind']} {c['value']}: {', '.join(c['notes'])}" for c in checked)
    return {"status": "pass", "reason": f"{passed}.", "identifiers": checked}


# --- print-and-rescan -------------------------------------------------------------

# Watermarks phone scanning apps print on every page they produce.
_PHONE_SCANNER_RE = re.compile(
    r"scanned\s+with\s+camscanner|\bcamscanner\b|\badobe\s+scan\b|\bmicrosoft\s+lens\b|\boffice\s+lens\b|"
    r"\bgenius\s+scan\b|\btapscanner\b|\btap\s+scanner\b",
    re.IGNORECASE,
)
# Hardware multifunction printers / office scanners, as named in a PDF's
# Producer / Creator.
_MFP_RE = re.compile(
    r"konica|minolta|bizhub|\bdevelop\b|\bineo\b|\bcanon\b|imagerunner|\bricoh\b|\bxerox\b|"
    r"\bhp\b|hewlett|\bkyocera\b|taskalfa|\bsharp\b|\bbrother\b|\bepson\b|\bmfp\d*\b",
    re.IGNORECASE,
)


def _check_rescan_conflict(pdf_info: dict[str, Any] | None, ocr_text: str | None) -> dict[str, Any]:
    """A phone-scanner watermark inside a PDF made by an office MFP: the page
    was scanned with a phone app, printed, and scanned again. Either alone is
    ordinary."""
    if not pdf_info:
        return {"status": "skipped", "reason": "PDF producer information not recorded for this document."}
    device = " / ".join(str(pdf_info[k]) for k in ("Producer", "Creator") if pdf_info.get(k))
    watermark = _PHONE_SCANNER_RE.search(ocr_text or "")
    mfp = _MFP_RE.search(device)
    if watermark and mfp:
        return _flag(
            f"Printed and re-scanned: the page carries a phone-scanner watermark ('{watermark.group(0)}') "
            f"but the PDF was produced by an office scanner ({device}). Digital edits made before printing "
            "would not be visible to error level analysis."
        )
    if watermark:
        return {
            "status": "pass",
            "reason": f"Phone-scanner watermark ('{watermark.group(0)}'), but no office scanner in the PDF metadata.",
        }
    return {"status": "pass", "reason": "No phone-scanner watermark on the page."}


# --- printed date vs the file's own dates ---------------------------------------------

_FILE_DATE_TOLERANCE = timedelta(days=1)
# Scanners and PCs with an unset clock write dates at or before this.
_UNSET_CLOCK_DATE = date(2000, 1, 1)


def _iso_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _check_document_date_vs_file_creation(
    core_fields: dict[str, Any], pdf_info: dict[str, Any] | None
) -> dict[str, Any]:
    """A PDF file cannot predate its own content: a printed date well after
    the file's CreationDate, in a file modified on or after that date, means
    an older file was edited and re-dated."""
    raw = (core_fields.get("date") or {}).get("value")
    printed = _parse_date(raw) if raw else None
    created = _iso_datetime((pdf_info or {}).get("CreationDate"))
    if printed is None:
        return {"status": "skipped", "reason": "No document date extracted."}
    if created is None:
        return {"status": "skipped", "reason": "PDF creation date not recorded for this document."}
    if created.date() <= _UNSET_CLOCK_DATE:
        return {"status": "skipped", "reason": f"PDF creation date {created.date().isoformat()} looks like an unset clock."}
    gap = printed - created.date()
    if gap <= _FILE_DATE_TOLERANCE:
        return {
            "status": "pass",
            "reason": f"Document date {printed.isoformat()} is not after the file's creation ({created.date().isoformat()}).",
        }
    modified = _iso_datetime((pdf_info or {}).get("ModDate"))
    if modified is None or modified.date() + _FILE_DATE_TOLERANCE < printed:
        return {
            "status": "pass",
            "reason": (
                f"The file was created {gap.days} days before its printed date {printed.isoformat()} but not "
                "modified on or after it — consistent with a document generated in advance."
            ),
        }
    return _flag(
        f"The document is dated {printed.isoformat()}, but its PDF file was created on "
        f"{created.date().isoformat()} — {gap.days} days earlier — and last modified on "
        f"{modified.date().isoformat()}. A file cannot predate its own content: an older document appears to "
        "have been edited and re-dated.",
        make_region(core_fields, "date", f"Field exception: Date ({raw} is after the file's creation)"),
    )


# A browser's print header, at the top of a printed web page: "8/18/23, 12:54 PM".
_PRINT_HEADER_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4}),?\s+\d{1,2}:\d{2}(?:\s*[AP]M)?\b", re.IGNORECASE)


def _print_dates(ocr_text: str | None) -> list[date]:
    """The browser print header's date, in each reading its digits allow
    (month/day first, as browsers in English print it, then day/month)."""
    first_lines = "\n".join((ocr_text or "").strip().splitlines()[:2])
    match = next((m for line in first_lines.splitlines() if (m := _PRINT_HEADER_RE.match(line))), None)
    if match is None:
        return []
    a, b, year = int(match[1]), int(match[2]), int(match[3])
    year += 2000 if year < 100 else 0
    readings = []
    for month, day in ((a, b), (b, a)):
        try:
            readings.append(date(year, month, day))
        except ValueError:
            continue
    return list(dict.fromkeys(readings))


def _day(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def _check_date_sequence(
    core_fields: dict[str, Any], pdf_info: dict[str, Any] | None, ocr_text: str | None
) -> dict[str, Any]:
    """Document date -> print date -> PDF creation, in order (module docstring)."""
    raw = (core_fields.get("date") or {}).get("value")
    dated = _parse_date(raw) if raw else None
    created_at = _iso_datetime((pdf_info or {}).get("CreationDate"))
    created = created_at.date() if created_at and created_at.date() > _UNSET_CLOCK_DATE else None
    readings = _print_dates(ocr_text)
    if not readings:
        return {"status": "skipped", "reason": "No print date on the page (no browser print header)."}

    def problems(printed: date) -> list[str]:
        found = []
        if dated and printed + _FILE_DATE_TOLERANCE < dated:
            found.append(f"printed {_day(printed)}, before its own date {_day(dated)}")
        if created and created + _FILE_DATE_TOLERANCE < printed:
            found.append(f"the PDF was created {_day(created)}, before the page was printed ({_day(printed)})")
        return found

    printed = next((r for r in readings if not problems(r)), None)
    if printed is not None:
        chain = ([f"dated {_day(dated)}"] if dated else []) + [f"printed {_day(printed)}"] + (
            [f"PDF created {_day(created)}"] if created else []
        )
        return {"status": "pass", "reason": " → ".join(chain) + ": in order."}
    return _flag(
        "The dates are out of order: " + "; ".join(problems(readings[0])) + ".",
        make_region(core_fields, "date", f"Field exception: Date ({raw} vs the page's print date)"),
    )


def _check_fake_scan_watermark(pdf_info: dict[str, Any] | None, ocr_text: str | None) -> dict[str, Any]:
    """A scanner app's watermark on a file with no raster image (module docstring)."""
    watermark = _PHONE_SCANNER_RE.search(ocr_text or "")
    if not watermark:
        return {"status": "skipped", "reason": "No scanner-app watermark on the page."}
    images = (pdf_info or {}).get("image_count")
    if images is None:
        return {"status": "skipped", "reason": "The file's images were not counted for this document."}
    if images == 0:
        return _flag(
            f"The page says '{watermark.group(0)}', but the file contains no image at all — nothing was scanned. "
            "The line is typed text, added to make the document look like a scan of a real paper."
        )
    return {"status": "pass", "reason": f"'{watermark.group(0)}' on a file with {images} scanned image(s)."}


_SYNTHETIC_MATERIALS = {"text": "live text", "vector": "drawn lines", "text_and_vector": "live text and drawn lines"}


def _synthetic_stamps(stamps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [s for s in stamps or [] if s.get("material") in _SYNTHETIC_MATERIALS]


def _check_stamp_authenticity(stamps: list[dict[str, Any]] | None) -> dict[str, Any]:
    """A stamp typed into the file rather than an image of an ink stamp."""
    if stamps is None:
        return {"status": "skipped", "reason": "Signature/stamp detection has not finished for this document."}
    if not stamps:
        return {"status": "skipped", "reason": "No stamp on this document."}
    if all("material" not in s for s in stamps):
        return {"status": "skipped", "reason": "The stamp's material was not recorded for this document."}
    fake = _synthetic_stamps(stamps)
    if not fake:
        made = sorted({s.get("material", "unknown") for s in stamps})
        return {"status": "pass", "reason": f"The stamp is {' / '.join(made)} — an image of a real stamp."}
    pages = sorted({(s.get("bounding_box") or {}).get("page") for s in fake} - {None})
    made = " / ".join(sorted({_SYNTHETIC_MATERIALS[s["material"]] for s in fake}))
    text = next((s.get("text") for s in fake if s.get("text")), "")
    return _flag(
        f"The stamp{' on page ' + ', '.join(map(str, pages)) if pages else ''}"
        f"{' (' + repr(text) + ')' if text else ''} is {made} in the file, not an image of an ink stamp: it was "
        "typed into the document, which a real stamped paper never is."
    )


def _check_synthetic_stamp_unsigned(
    stamps: list[dict[str, Any]] | None, signatures: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """A typed-in stamp, and no signature anywhere either."""
    if stamps is None or signatures is None:
        return {"status": "skipped", "reason": "Signature/stamp detection has not finished for this document."}
    if not _synthetic_stamps(stamps):
        return {"status": "skipped", "reason": "No typed-in stamp."}
    if signatures:
        return {"status": "pass", "reason": "A signature is present beside the typed-in stamp."}
    return _flag("Besides the typed-in stamp, the document carries no signature at all.")


_TAX_INVOICE_RE = re.compile(r"\btax\s+invoice\b|فاتورة\s+ضريبية", re.IGNORECASE)
_UAE_TRN_RE = re.compile(r"(?<!\d)100\d{12}(?!\d)")
# A tax registration number as printed: its label, then a number of about
# its length (any country's; whether it is a valid UAE TRN is
# iban_trn_validation's question — here only whether one is shown).
_LABELLED_TRN_RE = re.compile(
    r"(?:\bTRN\b|T\.R\.N|tax\s+registration(?:\s+(?:no|number))?|\bVAT\s+(?:no|number|reg(?:istration)?)\b"
    r"|الرقم\s+الضريبي|رقم\s+التسجيل\s+الضريبي)[^\d\n]{0,30}(\d[\d\s-]{8,22}\d)",
    re.IGNORECASE,
)
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _check_tax_invoice_trn(
    core_fields: dict[str, Any], additional_fields: list[dict[str, Any]], ocr_text: str | None
) -> dict[str, Any]:
    """A TAX INVOICE must show the supplier's TRN (module docstring): a
    labelled 15-digit number, in Western or Arabic-Indic digits, or a UAE
    TRN (100…) anywhere."""
    title = _TAX_INVOICE_RE.search(ocr_text or "")
    if not title:
        return {"status": "skipped", "reason": "Not titled a tax invoice."}
    values = [str(f.get("value") or "") for f in additional_fields if isinstance(f, dict)]
    text = " ".join([ocr_text or "", *values]).translate(_ARABIC_INDIC)
    labelled = next(
        (digits for m in _LABELLED_TRN_RE.finditer(text) if 10 <= len(digits := re.sub(r"\D", "", m.group(1))) <= 17),
        None,
    )
    found = labelled or (m.group(0) if (m := _UAE_TRN_RE.search(re.sub(r"[\s-]", "", text))) else None)
    if found:
        return {"status": "pass", "reason": f"Tax invoice with TRN {found}."}
    return _flag(
        f"Titled '{title.group(0)}' but no TRN appears anywhere on it. A UAE tax invoice must carry the "
        "supplier's 15-digit Tax Registration Number."
    )


def _invoice_label(invoice: dict[str, Any]) -> str:
    core = invoice.get("core_fields") or {}
    ref = (core.get("reference_number") or {}).get("value") or "?"
    dated = _parse_date((core.get("date") or {}).get("value") or "")
    amount = _num((core.get("amount") or {}).get("value"))
    parts = [f"p.{invoice.get('page')}", str(ref)]
    if dated:
        parts.append(dated.strftime("%d %b %Y"))
    if amount is not None:
        parts.append(_money(amount))
    return " ".join(parts[:2]) + (f" ({', '.join(parts[2:])})" if len(parts) > 2 else "")


def _check_multiple_invoices(invoices: list[dict[str, Any]]) -> dict[str, Any]:
    """Several invoices in one file; two for the same month are flagged."""
    if len(invoices) < 2:
        return {"status": "skipped", "reason": "One invoice in the file."}
    by_month: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for invoice in invoices:
        dated = _parse_date(((invoice.get("core_fields") or {}).get("date") or {}).get("value") or "")
        if dated:
            by_month.setdefault((dated.year, dated.month), []).append(invoice)
    listed = "; ".join(_invoice_label(i) for i in invoices)
    same = [group for group in by_month.values() if len(group) > 1]
    if same:
        month = _parse_date((same[0][0]["core_fields"]["date"])["value"]).strftime("%b %Y")
        return _flag(
            f"{len(invoices)} invoices in one file, {len(same[0])} of them for {month}: "
            + "; ".join(_invoice_label(i) for i in same[0])
            + f". All invoices: {listed}."
        )
    return {"status": "pass", "reason": f"{len(invoices)} invoices in one file, each for a different month: {listed}."}


# The arithmetic sub-checks run on each invoice of a multi-invoice file.
_PER_INVOICE_CHECKS = {
    "line_item_arithmetic": "line item",
    "subtotal_line_item_consistency": "line items vs total",
    "total_tax_consistency": "total and tax",
    "tax_rate_consistency": "tax rate",
}


def _check_per_invoice(invoices: list[dict[str, Any]], flagged_already: set[str] = frozenset()) -> dict[str, Any]:
    """Each invoice of a multi-invoice file checked on its own. A kind of
    problem the document's own sub-checks already flag is listed but not
    flagged again (`flagged_already`): one cause, scored once."""
    if len(invoices) < 2:
        return {"status": "skipped", "reason": "One invoice in the file."}
    problems = []
    repeats = []
    for invoice in invoices:
        core = invoice.get("core_fields") or {}
        lines = [i for i in invoice.get("line_items") or [] if isinstance(i, dict)]
        results = {
            "line_item_arithmetic": _check_line_item_arithmetic(lines),
            "subtotal_line_item_consistency": _check_subtotal_line_item_consistency(core, [], lines),
            "total_tax_consistency": _check_total_tax_consistency(core),
            "tax_rate_consistency": _check_tax_rate_consistency(core, lines, invoice.get("ocr_text")),
        }
        for name, result in results.items():
            if result["status"] == "flag":
                line = f"{_invoice_label(invoice)} — {_PER_INVOICE_CHECKS[name]}: {result['reason']}"
                (repeats if name in flagged_already else problems).append(line)
    kinds = sorted({_PER_INVOICE_CHECKS[n] for n in flagged_already & set(_PER_INVOICE_CHECKS)})
    also = (
        f" The same {', '.join(kinds)} problem the document's own check flags repeats on: " + " | ".join(repeats) + "."
        if repeats else ""
    )
    if problems:
        return _flag(f"{len(problems)} problem(s) across the file's invoices: " + " | ".join(problems) + also)
    return {"status": "pass", "reason": f"No other problem in the {len(invoices)} invoices taken one by one.{also}"}


# --- stamp vs issuer ------------------------------------------------------------------

# Words on a stamp or in an issuer line that name a place, an address or a
# legal form, not the organisation.
_STAMP_NOISE_WORDS = {
    "po", "p", "o", "box", "pob", "tel", "fax", "br", "branch", "llc", "l", "c", "fze", "fzc", "fz", "co",
    "est", "ltd", "inc", "uae", "u", "a", "e", "abu", "dhabi", "dubai", "sharjah", "ajman", "fujairah",
    "ras", "khaimah", "umm", "quwain", "ain", "al", "the", "of", "and",
    "ص", "ب", "ص.ب", "هاتف", "فاكس", "ذ.م.م", "ش.ذ.م.م", "أبوظبي", "ابوظبي", "دبي", "الشارقة", "عجمان", "العين",
}
# Abbreviations written either way on stamps and in issuer names.
_NAME_ABBREVIATIONS = {
    "mbz": "mohamed bin zayed", "mbr": "mohammed bin rashid", "intl": "international", "int'l": "international",
    "sch": "school", "acad": "academy", "univ": "university", "pvt": "private", "est": "establishment",
}
# Words that say what kind of organisation it is, not which one.
_GENERIC_NAME_WORDS = {"school", "schools", "campus", "llc", "branch", "pvt", "private"}
_ARABIC_RE = re.compile("[؀-ۿ]")
_STAMP_MATCH_THRESHOLD = 80.0


def _name_words(text: str) -> dict[str, str]:
    """Organisation words of `text`, per writing system ("latin"/"arabic")."""
    by_script: dict[str, list[str]] = {"latin": [], "arabic": []}
    for word in re.findall(r"[^\W\d_]+(?:\.[^\W\d_]+)*", text or ""):
        lowered = word.casefold().strip(".")
        for part in _NAME_ABBREVIATIONS.get(lowered, lowered).split():
            if part in _STAMP_NOISE_WORDS or part in _GENERIC_NAME_WORDS or len(part) < 2:
                continue
            by_script["arabic" if _ARABIC_RE.search(part) else "latin"].append(part)
    return {script: " ".join(words) for script, words in by_script.items() if words}


def _check_stamp_issuer_consistency(
    core_fields: dict[str, Any], stamps: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """The stamp names the issuer (module docstring)."""
    issuer = (core_fields.get("issuer") or {}).get("value")
    read = [s for s in stamps or [] if (s.get("text") or "").strip()]
    if stamps is None:
        return {"status": "skipped", "reason": "Signature/stamp detection has not finished for this document."}
    if not read:
        return {"status": "skipped", "reason": "No stamp text was read on this document."}
    if not issuer:
        return {"status": "skipped", "reason": "No issuer extracted to compare the stamp against."}
    issuer_words = _name_words(str(issuer))
    compared = []
    for stamp in read:
        stamp_words = _name_words(stamp["text"])
        for script in stamp_words.keys() & issuer_words.keys():
            score = fuzz.token_set_ratio(stamp_words[script], issuer_words[script])
            compared.append((score, stamp["text"]))
    if not compared:
        return {
            "status": "skipped",
            "reason": f"The stamp ('{read[0]['text']}') and the issuer ('{issuer}') share no writing system to compare.",
        }
    score, text = max(compared)
    if score >= _STAMP_MATCH_THRESHOLD:
        return {"status": "pass", "reason": f"The stamp ('{text}') names the issuer '{issuer}'.", "score": round(score, 1)}
    stamp_box = next((s.get("bounding_box") for s in read if s["text"] == text), None)
    result = _flag(
        f"The stamp reads '{text}', which does not name the issuer '{issuer}' (similarity {score:.0f}/100): "
        "the stamp may belong to another organisation.",
        make_region(core_fields, "issuer", f"Field exception: Issuer (stamp reads '{text}')"),
    )
    result["score"] = round(score, 1)
    if stamp_box:
        result["stamp_bounding_box"] = stamp_box
    return result


# --- billing periods vs quantity ----------------------------------------------------

_PERIOD_UNITS = {"MONTH", "MON", "MTH"}


def _check_period_quantity_consistency(line_items: list[dict[str, Any]]) -> dict[str, Any]:
    """A line that lists the months it bills ("(October-November-December)
    DUE") and a multiplier ("X 4 MONTH") must charge for as many months as it
    lists as due (app/services/line_item_parsing.py reads both)."""
    checked = 0
    wrong: list[str] = []
    regions: list[dict[str, Any] | None] = []
    for index, item in enumerate(line_items, start=1):
        quantity = _num(item.get("quantity"))
        unit = (item.get("unit") or "").upper()
        due = item.get("due_periods") or []
        if not due and not item.get("paid_periods"):
            due = item.get("unmarked_periods") or []
        if quantity is None or unit not in _PERIOD_UNITS or not due:
            continue
        checked += 1
        if abs(quantity - len(due)) < 1e-9:
            continue
        paid = item.get("paid_periods") or []
        paid_note = f"; {', '.join(paid)} marked paid" if paid else ""
        wrong.append(
            f"{_line_label(index, item)}: {len(due)} months listed as due ({', '.join(due)}{paid_note}), "
            f"× {_qty(quantity)} charged"
        )
        regions.append(
            make_located_region(
                item, f"line_item_{index}", f"Line {index}", _money(_num(item.get("line_total")) or 0),
                f"Field exception: Line {index} ({len(due)} months due, × {_qty(quantity)} charged)",
            )
        )
    if not checked:
        return {"status": "skipped", "reason": "No line lists its billing months and a month multiplier."}
    if wrong:
        return _flag("; ".join(wrong) + ".", *regions)
    return {"status": "pass", "reason": f"All {checked} line(s) charge for the months they list as due."}


def validate_fields(
    extracted_fields: dict[str, Any],
    *,
    ocr_text: str | None = None,
    stamps: list[dict[str, Any]] | None = None,
    signatures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all field-validation sub-checks against one document's
    `extracted_fields` (and its OCR text, for the checks that read printed
    marks; and the stamps and signatures signature/stamp detection found,
    None if it has not run). Returns {"result": "pass"|"flag", "details": {...}} ready to
    store directly in a `document_checks.result` jsonb column."""
    core_fields = extracted_fields.get("core_fields") or {}
    additional_fields = extracted_fields.get("additional_fields") or []
    line_items = [i for i in extracted_fields.get("line_items") or [] if isinstance(i, dict)]
    invoices = [i for i in extracted_fields.get("invoices") or [] if isinstance(i, dict)]

    details = {
        "date_in_future": _check_date_in_future(core_fields),
        "line_item_arithmetic": _check_line_item_arithmetic(line_items),
        "subtotal_line_item_consistency": _check_subtotal_line_item_consistency(
            core_fields, additional_fields, line_items, extracted_fields.get("column_totals")
        ),
        "total_tax_consistency": _check_total_tax_consistency(core_fields),
        "tax_rate_consistency": _check_tax_rate_consistency(core_fields, line_items, ocr_text),
        "amount_in_words_consistency": _check_amount_in_words_consistency(
            core_fields, extracted_fields.get("amount_in_words")
        ),
        "reference_number_format": _check_reference_number_format(core_fields),
        "iban_trn_validation": _check_iban_trn_validation(core_fields, additional_fields, ocr_text),
        "rescan_conflict": _check_rescan_conflict(extracted_fields.get("pdf_info"), ocr_text),
        "period_quantity_consistency": _check_period_quantity_consistency(line_items),
        "document_date_vs_file_creation": _check_document_date_vs_file_creation(
            core_fields, extracted_fields.get("pdf_info")
        ),
        "date_sequence": _check_date_sequence(core_fields, extracted_fields.get("pdf_info"), ocr_text),
        "installment_consistency": _check_installment_consistency(line_items),
        "stamp_issuer_consistency": _check_stamp_issuer_consistency(core_fields, stamps),
        "stamp_authenticity": _check_stamp_authenticity(stamps),
        "synthetic_stamp_unsigned": _check_synthetic_stamp_unsigned(stamps, signatures),
        "fake_scan_watermark": _check_fake_scan_watermark(extracted_fields.get("pdf_info"), ocr_text),
        "tax_invoice_trn": _check_tax_invoice_trn(core_fields, additional_fields, ocr_text),
        "multiple_invoices": _check_multiple_invoices(invoices),
    }
    details["per_invoice_checks"] = _check_per_invoice(
        invoices, {name for name, sub in details.items() if sub["status"] == "flag"}
    )
    overall = "flag" if any(sub["status"] == "flag" for sub in details.values()) else "pass"
    return {"result": overall, "details": details}
