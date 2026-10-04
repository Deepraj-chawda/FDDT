"""
Date/amount/consistency validation (CLAUDE.md section 3.1,
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

Four independent sub-checks, each producing its own {status, reason} in
the returned `details` dict:
  - date_in_future: the document's own `date` core field is after today.
  - subtotal_line_item_consistency: the `subtotal` core field vs. the
    sum of any additional_fields that look like per-line-item totals.
    Best-effort — extraction is template-free (CLAUDE.md section 3.6),
    so line items don't have a fixed field name; this looks for
    additional fields whose name suggests a line-item total. Skipped
    when there's no subtotal to compare against, or no such fields
    found.
  - total_tax_consistency: the `amount` core field (the grand total) vs.
    `subtotal + tax_amount` when a tax amount is shown separately, or
    vs. `subtotal` alone when it isn't (a document with no visible tax
    line should validate total == subtotal, not be flagged for
    "missing" tax). Skipped when there's no subtotal to check against.
  - reference_number_format: the `reference_number` core field looks
    like a plausible reference/invoice number (contains a digit, no
    stray/garbled characters) rather than obviously-malformed
    extraction output.

A sub-check that doesn't apply (field missing, nothing to compare) is
reported as "skipped" rather than "pass" or "flag" — it isn't evidence of
anything either way. The overall `result` is "flag" if any sub-check
flags, else "pass".
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.services.field_regions import format_field_value, make_region

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


def _check_subtotal_line_item_consistency(
    core_fields: dict[str, Any], additional_fields: list[dict[str, Any]]
) -> dict[str, Any]:
    subtotal = (core_fields.get("subtotal") or {}).get("value")
    line_items = _line_item_amounts(additional_fields)

    if subtotal is None or not line_items:
        return {
            "status": "skipped",
            "reason": "No subtotal and/or line-item amount fields to compare.",
        }

    line_item_sum = sum(amount for _, amount in line_items)
    # Small tolerance for rounding differences, not a strict equality check.
    if abs(line_item_sum - subtotal) > max(0.01, subtotal * 0.01):
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

    if abs(expected - total) > max(0.01, total * 0.01):
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

    details = {
        "date_in_future": _check_date_in_future(core_fields),
        "subtotal_line_item_consistency": _check_subtotal_line_item_consistency(
            core_fields, additional_fields
        ),
        "total_tax_consistency": _check_total_tax_consistency(core_fields),
        "reference_number_format": _check_reference_number_format(core_fields),
    }
    overall = "flag" if any(sub["status"] == "flag" for sub in details.values()) else "pass"
    return {"result": overall, "details": details}
