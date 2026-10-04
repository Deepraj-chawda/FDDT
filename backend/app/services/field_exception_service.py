"""
Field-level exception regions: turns the two rule-based field exceptions —
a failed field-validation sub-check and a cross-document mismatch — into
highlight regions (see app/services/field_regions.py for the shape).

  * Field validation embeds its regions in the flagged sub-check itself when
    it runs (app/services/field_validation_service.py). `with_field_regions`
    fills them in for check rows stored BEFORE field locations existed, by
    re-deriving them from the document's now-known field boxes — read-time
    only, the stored row is not modified.
  * Cross-document findings store which two documents disagreed on which
    field; the boxes live on the documents' own extracted fields, so the
    regions are derived at read time (`cross_document_regions`). A mismatch
    yields one region on EACH document, each captioned with what the other
    document showed, so a reviewer sees both without flipping pages.

Regions exist only where the field's location is known; an exception whose
field couldn't be located is still reported, just without a highlight.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.services.field_regions import field_label, format_field_value, valid_box
from app.services.field_validation_service import validate_fields

# Findings at these severities are real exceptions; "low"/"info" cross-
# document differences (e.g. an invoice date vs its later payment date) are
# expected between a claim and its evidence and are context, not exceptions.
EXCEPTION_SEVERITIES = ("medium", "high")


def with_field_regions(result: dict[str, Any] | None, extracted_fields: dict[str, Any] | None) -> dict[str, Any] | None:
    """`result` (a stored field_validation check result) with `regions` added
    to any flagged sub-check that lacks them. Returns a new dict; never
    mutates the input."""
    if not isinstance(result, dict) or not isinstance(result.get("details"), dict) or not extracted_fields:
        return result
    details = result["details"]
    needs = [n for n, sub in details.items() if isinstance(sub, dict) and sub.get("status") == "flag" and "regions" not in sub]
    if not needs:
        return result
    recomputed = validate_fields(extracted_fields).get("details", {})
    enriched = dict(details)
    for name in needs:
        regions = (recomputed.get(name) or {}).get("regions")
        if regions:
            enriched[name] = {**details[name], "regions": regions}
    return {**result, "details": enriched}


def _core(document: Any) -> dict[str, Any]:
    return ((getattr(document, "extracted_fields", None) or {}).get("core_fields")) or {}


def cross_document_regions(
    field_name: str, document_ids: Iterable[str] | None, documents_by_id: dict[str, Any]
) -> list[dict[str, Any]]:
    """One region per involved document that has a located `field_name`.

    Each carries what THIS document shows, what each other involved document
    showed, and a caption stating both — e.g. "Field mismatch: Total amount
    (9,030.00 USD vs 7,250.00 USD)" where the first value is this document's.
    """
    involved = [documents_by_id[str(i)] for i in (document_ids or []) if str(i) in documents_by_id]
    values = {str(d.id): format_field_value(field_name, _core(d).get(field_name)) for d in involved}
    regions: list[dict[str, Any]] = []
    for doc in involved:
        box = valid_box((_core(doc).get(field_name) or {}).get("bounding_box"))
        if box is None:
            continue
        others = [
            {"document_id": str(o.id), "document_filename": o.original_filename, "value": values[str(o.id)]}
            for o in involved if o is not doc
        ]
        this_value = values[str(doc.id)]
        regions.append(
            {
                "document_id": str(doc.id),
                "document_filename": doc.original_filename,
                "field": field_name,
                "label": field_label(field_name),
                "value": this_value,
                "caption": f"Field mismatch: {field_label(field_name)} ({this_value} vs "
                + " / ".join(o["value"] for o in others)
                + ")",
                "other": others,
                "bounding_box": box,
            }
        )
    return regions
