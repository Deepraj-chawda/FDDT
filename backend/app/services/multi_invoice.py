"""
Files holding several invoices (one per page, or a few pages each): the
extraction model reads the whole file as one document and takes the first
invoice's fields, so each invoice is found and extracted on its own here.

An invoice starts on a page carrying an invoice number ("Invoice #:
MLC15112287877", "Invoice No. 0400") different from the previous page's;
pages without one belong to the invoice before them. With two or more
distinct numbers, each invoice's own pages are sent to the extraction model
separately, and kept in `extracted_fields["invoices"]`:

    [{"page": 1, "pages": [1], "invoice_number": "...", "ocr_text": "...",
      "core_fields": {...}, "line_items": [...]}, ...]

Field validation then checks each invoice's arithmetic on its own and
flags two invoices for the same month (app/services/field_validation_service.py,
`multiple_invoices` / `per_invoice_checks`); duplicate detection compares a
matched page against the invoice on that page.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.extraction_service import classify_and_extract

_INVOICE_NUMBER_RE = re.compile(
    r"\b(?:tax\s+)?invoice\s*(?:no\.?|number|num\.?|#)\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{3,})",
    re.IGNORECASE,
)
# More invoices than this in one file are not extracted page by page (cost).
MAX_INVOICES = 12


def page_text(page: Any) -> str:
    """One OCR page's text, line by line."""
    lines = getattr(page, "lines", None) or []
    if lines:
        return "\n".join(line.text for line in lines)
    return " ".join(w.text for w in getattr(page, "words", None) or [])


def page_texts(ocr_pages: list[Any], pdf_bytes: bytes | None = None) -> list[tuple[int, str]]:
    """(page number, text) for every page: the PDF's own text layer where
    the page has one (exact, and not limited to the pages OCR covered — a
    free-tier OCR reads only the first two), else the OCR page."""
    by_number = {p.page_number: page_text(p) for p in ocr_pages or []}
    layer: dict[int, str] = {}
    if pdf_bytes:
        try:
            import pymupdf

            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                layer = {i + 1: page.get_text("text") for i, page in enumerate(doc)}
        except Exception:  # noqa: BLE001 - OCR text only
            layer = {}
    numbers = sorted(set(by_number) | {n for n, t in layer.items() if t.strip()})
    return [(n, layer[n] if layer.get(n, "").strip() else by_number.get(n, "")) for n in numbers]


def split_invoices(ocr_pages: list[Any], pdf_bytes: bytes | None = None) -> list[dict[str, Any]]:
    """[{"invoice_number", "pages", "ocr_text"}] — one per invoice, when the
    file holds two or more; else []."""
    groups: list[dict[str, Any]] = []
    for page_number, text in page_texts(ocr_pages, pdf_bytes):
        match = _INVOICE_NUMBER_RE.search(text)
        number = match.group(1).upper() if match else None
        if number and (not groups or groups[-1]["invoice_number"] != number):
            groups.append({"invoice_number": number, "pages": [page_number], "ocr_text": text})
        elif groups:
            groups[-1]["pages"].append(page_number)
            groups[-1]["ocr_text"] += "\n" + text
    numbers = {g["invoice_number"] for g in groups}
    return groups if 2 <= len(numbers) and len(groups) <= MAX_INVOICES else []


def extract_invoices(llm_service: Any, ocr_pages: list[Any], pdf_bytes: bytes | None = None) -> list[dict[str, Any]]:
    """Each invoice of a multi-invoice file extracted from its own pages
    (module docstring); [] for a single-invoice file."""
    invoices = []
    for group in split_invoices(ocr_pages, pdf_bytes):
        analysis = classify_and_extract(llm_service, group["ocr_text"])
        invoices.append({
            "page": group["pages"][0],
            "pages": group["pages"],
            "invoice_number": group["invoice_number"],
            "ocr_text": group["ocr_text"],
            "core_fields": analysis.core_fields_as_dict(),
            "additional_fields": [f.model_dump() for f in analysis.additional_fields],
            "line_items": [item.model_dump() for item in analysis.line_items],
        })
    return invoices


def invoice_on_page(extracted_fields: dict[str, Any] | None, page: int) -> dict[str, Any] | None:
    """The invoice printed on `page` of a multi-invoice file, if any."""
    for invoice in (extracted_fields or {}).get("invoices") or []:
        if page in (invoice.get("pages") or [invoice.get("page")]):
            return invoice
    return None
