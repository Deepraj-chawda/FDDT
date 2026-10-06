"""
Shared helpers for field-level highlight regions: labels, value formatting,
bounding-box validation and building one region dict.

A "region" is the shape attached to a rule-based field exception (a failed
field-validation sub-check, or one side of a cross-document mismatch) so it
can be drawn like any other finding:

    {"field": "amount", "label": "Total amount", "value": "4,520.00 AED",
     "caption": "Field exception: Total amount (...)",
     "bounding_box": {"page", "x", "y", "width", "height"}}   # normalized 0-1

The box itself comes from the field's stored `bounding_box`
(app/services/field_locator_service.py). Kept dependency-free so both
app/services/field_validation_service.py and app/services/
field_exception_service.py can import it without a cycle.
"""
from __future__ import annotations

from typing import Any

FIELD_LABELS = {
    "amount": "Total amount",
    "subtotal": "Subtotal",
    "tax_amount": "Tax amount",
    "tax_rate": "Tax rate",
    "date": "Date",
    "issuer": "Issuer",
    "reference_number": "Reference number",
}


def humanize(value: str) -> str:
    text = value.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else text


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name, humanize(name))


def format_field_value(name: str, field: dict[str, Any] | None) -> str:
    value = (field or {}).get("value")
    if value is None or value == "":
        return "not found"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if name == "tax_rate":
            return f"{value:g}%"
        text = f"{value:,.2f}"
        currency = (field or {}).get("currency")
        return f"{text} {currency}" if currency else text
    return str(value)


def valid_box(raw: Any) -> dict[str, float | int] | None:
    """A stored bounding box as a clean {page, x, y, width, height} dict, or
    None if it is missing/degenerate. Boxes are normalized 0-1 page
    fractions, `page` is 1-based."""
    if not isinstance(raw, dict):
        return None
    try:
        page = int(raw["page"])
        x, y = float(raw["x"]), float(raw["y"])
        w, h = float(raw["width"]), float(raw["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if page < 1 or w <= 0 or h <= 0:
        return None
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w <= 0 or h <= 0:
        return None
    return {"page": page, "x": round(x, 4), "y": round(y, 4), "width": round(w, 4), "height": round(h, 4)}


def make_located_region(
    located: dict[str, Any] | None, field: str, label: str, value: str, caption: str
) -> dict[str, Any] | None:
    """A region for any located value that is not a core field (a line item,
    the amount in words), from its own stored `bounding_box`; None if it has
    no location."""
    box = valid_box((located or {}).get("bounding_box"))
    if box is None:
        return None
    return {"field": field, "label": label, "value": value, "caption": caption, "bounding_box": box}


def make_region(core_fields: dict[str, Any], name: str, caption: str) -> dict[str, Any] | None:
    """The region for core field `name`, or None if that field has no
    stored location (the exception is still reported, just not drawn)."""
    field = core_fields.get(name)
    box = valid_box((field or {}).get("bounding_box"))
    if box is None:
        return None
    return {
        "field": name,
        "label": field_label(name),
        "value": format_field_value(name, field),
        "caption": caption,
        "bounding_box": box,
    }
