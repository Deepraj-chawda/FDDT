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
    total (less tax when tax is shown). Uses the structured `line_items`
    extraction; documents extracted before it existed fall back to
    additional_fields whose name suggests a line-item total, against the
    subtotal only.
  - total_tax_consistency: the `amount` core field (the grand total) vs.
    `subtotal + tax_amount` when a tax amount is shown separately, or
    vs. `subtotal` alone when it isn't (a document with no visible tax
    line should validate total == subtotal, not be flagged for
    "missing" tax). Skipped when there's no subtotal to check against.
  - tax_rate_consistency: the tax amount equals the taxable base x the
    stated tax rate.
  - amount_in_words_consistency: the amount written out in words equals
    the total (or the subtotal). English words are read here
    (app/services/amount_words.py), not taken from the extraction model,
    which tends to "correct" words that contradict the figures.
  - reference_number_format: the `reference_number` core field looks
    like a plausible reference/invoice number (contains a digit, no
    stray/garbled characters) rather than obviously-malformed
    extraction output.

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
from datetime import date, datetime
from typing import Any

from app.services.amount_words import parse_english_amount
from app.services.field_regions import format_field_value, make_located_region, make_region

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


def _check_line_items_sum(core_fields: dict[str, Any], line_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The structured line-item version of subtotal_line_item_consistency;
    None when there are no structured line totals to use."""
    priced = [(i, item, _num(item.get("line_total"))) for i, item in enumerate(line_items, start=1)]
    priced = [(i, item, total) for i, item, total in priced if total is not None]
    counted = [(i, item, total) for i, item, total in priced if item.get("counts_toward_total", True)]
    if not counted:
        return None
    target = _sum_target(core_fields)
    if target is None:
        return {"status": "skipped", "reason": "No subtotal or total to compare the line items against."}
    field, expected, label = target
    counted_sum = sum(total for _, _, total in counted)
    allowance = _rounding_allowance(len(priced) + 1)
    if abs(counted_sum - expected) <= allowance:
        return {"status": "pass", "reason": f"The {len(counted)} line item(s) add up to the {label}."}
    # The extraction's "counts toward total" call is a judgment: if every
    # priced line together matches instead, that is not an inconsistency.
    all_sum = sum(total for _, _, total in priced)
    if len(priced) != len(counted) and abs(all_sum - expected) <= allowance:
        return {"status": "pass", "reason": f"The {len(priced)} line item(s) add up to the {label}."}
    terms = " + ".join(_money(total) for _, _, total in counted)
    return _flag(
        f"The line items add up to {_money(counted_sum)} ({terms}), but the document states the {label} — "
        f"a difference of {_money(expected - counted_sum)}.",
        make_region(
            core_fields, field,
            f"Field exception: {'Subtotal' if field == 'subtotal' else 'Total amount'} "
            f"({format_field_value(field, core_fields.get(field))} vs line items {_money(counted_sum)})",
        ),
    )


def _check_subtotal_line_item_consistency(
    core_fields: dict[str, Any], additional_fields: list[dict[str, Any]],
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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


def _check_tax_rate_consistency(core_fields: dict[str, Any]) -> dict[str, Any]:
    rate = _num((core_fields.get("tax_rate") or {}).get("value"))
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
    return _flag(
        f"Tax {_money(tax)} is not {rate:g}% of the {base_label} (expected {_money(expected)}).",
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


def validate_fields(extracted_fields: dict[str, Any]) -> dict[str, Any]:
    """Run all field-validation sub-checks against one document's
    `extracted_fields`. Returns {"result": "pass"|"flag", "details": {...}}
    ready to store directly in a `document_checks.result` jsonb column."""
    core_fields = extracted_fields.get("core_fields") or {}
    additional_fields = extracted_fields.get("additional_fields") or []
    line_items = [i for i in extracted_fields.get("line_items") or [] if isinstance(i, dict)]

    details = {
        "date_in_future": _check_date_in_future(core_fields),
        "line_item_arithmetic": _check_line_item_arithmetic(line_items),
        "subtotal_line_item_consistency": _check_subtotal_line_item_consistency(
            core_fields, additional_fields, line_items
        ),
        "total_tax_consistency": _check_total_tax_consistency(core_fields),
        "tax_rate_consistency": _check_tax_rate_consistency(core_fields),
        "amount_in_words_consistency": _check_amount_in_words_consistency(
            core_fields, extracted_fields.get("amount_in_words")
        ),
        "reference_number_format": _check_reference_number_format(core_fields),
    }
    overall = "flag" if any(sub["status"] == "flag" for sub in details.values()) else "pass"
    return {"result": overall, "details": details}
