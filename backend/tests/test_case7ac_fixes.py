"""
Fixes from CASE-7AC9B30F (an invoice built as a web page — Bootstrap colours,
PDFium "Save as PDF", no creation date — with a typed "Scanned with
CamScanner" line, a stamp drawn as text and lines, three invoices in one
file, "VAT (5%)" over a 0.00 tax and no TRN on a TAX INVOICE).
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pikepdf
import pymupdf

from app.services.field_validation_service import validate_fields
from app.services.forensics.duplicate_check import refine_with_fields
from app.services.forensics.ela import run_ela_check
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.forensics.pdf_facts import annotate_region_materials, css_palette, raster_image_count
from app.services.multi_invoice import split_invoices


def _born_digital(*, stamp_as_image: bool = False, producer: str = "PDFium", colours: bool = True) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    blue, grey, pink = (13 / 255, 110 / 255, 253 / 255), (108 / 255, 117 / 255, 125 / 255), (214 / 255, 51 / 255, 132 / 255)
    page.insert_text((50, 80), "TAX INVOICE", fontsize=18, color=blue if colours else (0, 0, 0))
    page.insert_text((50, 110), "Invoice #: MLC15112287877", fontsize=10, color=grey if colours else (0, 0, 0))
    page.insert_text((50, 130), "VAT (5%) 0.00", fontsize=10, color=pink if colours else (0, 0, 0))
    page.insert_text((50, 800), "Scanned with CamScanner", fontsize=8)
    if stamp_as_image:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(80)
        page.insert_image(pymupdf.Rect(450, 600, 530, 680), pixmap=pix)
    else:
        page.draw_circle((490, 640), 38, color=grey, dashes="[3] 0")
        page.insert_text((462, 644), "VERIFIED", fontsize=9, color=grey)
    doc.set_metadata({"producer": producer, "creator": producer})
    data = doc.tobytes()
    doc.close()
    return data


STAMP_BOX = {"page": 1, "x": 450 / 595, "y": 600 / 842, "width": 80 / 595, "height": 80 / 842}


# --- facts ------------------------------------------------------------------------------


def test_pdf_facts_read_images_stamp_material_and_palette():
    typed = _born_digital()
    assert raster_image_count(typed) == 0
    [region] = annotate_region_materials([{"kind": "stamp", "bounding_box": STAMP_BOX}], typed)
    assert region["material"] == "text_and_vector"
    [image] = annotate_region_materials([{"kind": "stamp", "bounding_box": STAMP_BOX}], _born_digital(stamp_as_image=True))
    assert image["material"] == "image"
    framework, colours = css_palette(typed)
    assert framework == "Bootstrap 5" and {"#0d6efd", "#6c757d", "#d63384"} <= set(colours)
    assert css_palette(_born_digital(colours=False)) == (None, [])


# --- field validation ---------------------------------------------------------------------


def _fields(**extra) -> dict:
    core = {
        "amount": {"value": 10300.0}, "subtotal": {"value": 10300.0}, "tax_amount": {"value": 0.0},
        "date": {"value": "2022-11-15"}, "reference_number": {"value": "MLC15112287877"},
        "issuer": {"value": "PINK & BLUE NURSERY"},
    }
    core.update(extra.pop("core", {}))
    return {"core_fields": core, "line_items": [
        {"description": "Tuition fee", "quantity": 1, "unit_price": 10300, "line_total": 10300},
        {"description": "Registration", "quantity": 0, "unit_price": 0, "line_total": 0},
    ], **extra}


OCR = "TAX INVOICE\nInvoice #: MLC15112287877\nVAT (5%)\n0.00\nScanned with CamScanner Document Management"


def test_scanner_line_on_a_file_without_images_is_fake():
    details = validate_fields(_fields(pdf_info={"image_count": 0}), ocr_text=OCR)["details"]
    assert details["fake_scan_watermark"]["status"] == "flag"
    real = validate_fields(_fields(pdf_info={"image_count": 1}), ocr_text=OCR)["details"]
    assert real["fake_scan_watermark"]["status"] == "pass"


def test_vat_label_with_zero_tax_is_flagged_not_explained_by_a_zero_line():
    tax = validate_fields(_fields(), ocr_text=OCR)["details"]["tax_rate_consistency"]
    assert tax["status"] == "flag"
    assert tax["reason"].startswith("The printed label says 5% tax, but the tax is 0.00 on the subtotal 10,300.00")
    assert "5% would be 515.00" in tax["reason"]


def test_tax_invoice_needs_a_trn():
    assert validate_fields(_fields(), ocr_text=OCR)["details"]["tax_invoice_trn"]["status"] == "flag"
    with_trn = validate_fields(_fields(), ocr_text=OCR + "\nTRN: 100 2634 1840 0003")["details"]["tax_invoice_trn"]
    assert with_trn["status"] == "pass"
    plain = validate_fields(_fields(), ocr_text="INVOICE\nTotal 10,300")["details"]["tax_invoice_trn"]
    assert plain["status"] == "skipped"


def test_typed_stamp_and_no_signature():
    stamps = [{"kind": "stamp", "material": "text_and_vector", "text": "VERIFIED", "bounding_box": STAMP_BOX}]
    details = validate_fields(_fields(), ocr_text=OCR, stamps=stamps, signatures=[])["details"]
    assert details["stamp_authenticity"]["status"] == "flag"
    assert details["synthetic_stamp_unsigned"]["status"] == "flag"
    signed = validate_fields(_fields(), stamps=stamps, signatures=[{"kind": "signature"}])["details"]
    assert signed["synthetic_stamp_unsigned"]["status"] == "pass"
    ink = [{"kind": "stamp", "material": "image", "bounding_box": STAMP_BOX}]
    assert validate_fields(_fields(), stamps=ink, signatures=[])["details"]["stamp_authenticity"]["status"] == "pass"


def _invoice(page: int, number: str, date: str, amount: float, tax: float = 0.0) -> dict:
    return {
        "page": page, "pages": [page], "invoice_number": number, "ocr_text": f"Invoice #: {number}",
        "core_fields": {
            "reference_number": {"value": number}, "date": {"value": date}, "amount": {"value": amount + tax},
            "subtotal": {"value": amount}, "tax_amount": {"value": tax}, "tax_rate": {"value": 5.0},
        },
        "line_items": [{"description": "Tuition fee", "quantity": 1, "unit_price": amount, "line_total": amount}],
    }


def test_two_invoices_for_the_same_month_are_flagged():
    invoices = [_invoice(1, "MLC15112287877", "2022-11-15", 10300), _invoice(2, "MLC1311223264", "2022-11-13", 11500),
                _invoice(3, "MLC13102298412", "2022-10-13", 11800)]
    details = validate_fields(_fields(invoices=invoices), ocr_text=OCR)["details"]
    multiple = details["multiple_invoices"]
    assert multiple["status"] == "flag" and "2 of them for Nov 2022" in multiple["reason"]
    # The 5%-on-0 problem already flags on the document: repeated, not flagged again.
    assert details["per_invoice_checks"]["status"] == "pass"
    assert "repeats on" in details["per_invoice_checks"]["reason"]


def test_a_problem_only_in_another_invoice_is_flagged():
    invoices = [_invoice(1, "A100", "2022-09-01", 1000, tax=50), _invoice(2, "A101", "2022-10-01", 1000, tax=80)]
    fields = _fields(core={"tax_amount": {"value": 50.0}, "subtotal": {"value": 1000.0}, "amount": {"value": 1050.0},
                           "tax_rate": {"value": 5.0}}, invoices=invoices)
    fields["line_items"] = [{"description": "Tuition fee", "quantity": 1, "unit_price": 1000, "line_total": 1000}]
    per = validate_fields(fields)["details"]["per_invoice_checks"]
    assert per["status"] == "flag" and "p.2 A101" in per["reason"]


def test_invoices_are_split_by_their_numbers():
    pages = [SimpleNamespace(page_number=n, lines=[SimpleNamespace(text=t) for t in text.split("\n")], words=[])
             for n, text in [(1, "TAX INVOICE\nInvoice #: MLC1"), (2, "continued"), (3, "TAX INVOICE\nInvoice #: MLC2")]]
    groups = split_invoices(pages)
    assert [(g["invoice_number"], g["pages"]) for g in groups] == [("MLC1", [1, 2]), ("MLC2", [3])]
    assert split_invoices(pages[:2]) == []  # one invoice


# --- duplicates ------------------------------------------------------------------------------


def _dup(page: int, matched_page: int = 1, identical: bool = False) -> dict:
    return {"finding": "near_duplicate_page", "severity": "high", "page": page, "description": "...",
            "data": {"matched_document_id": "m", "matched_page": matched_page, "identical_file": identical,
                     "matched_document_filename": "caseimage1.pdf", "matched_case_number": "CASE-1"}}


def _doc_fields(ref: str, date: str, amount: float) -> dict:
    return {"core_fields": {"reference_number": {"value": ref}, "date": {"value": date}, "amount": {"value": amount}}}


def test_same_template_with_other_values_is_not_a_resubmission():
    result = {"result": "flag", "details": [_dup(1)]}
    refined = refine_with_fields(result, _doc_fields("MLC15112287877", "2022-11-15", 10300.0),
                                 {"m": _doc_fields("MLG15112257877", "2027-11-15", 10300.0)})
    [f] = refined["details"]
    assert f["finding"] == "same_template_page" and f["severity"] == "low" and refined["result"] == "pass"
    assert "invoice number MLC15112287877 vs MLG15112257877" in f["description"]


def test_same_values_is_a_resubmission():
    result = {"result": "flag", "details": [_dup(1)]}
    refined = refine_with_fields(result, _doc_fields("X-1", "2022-11-15", 10300.0), {"m": _doc_fields("X1", "2022-11-15", 10300.0)})
    [f] = refined["details"]
    assert f["finding"] == "near_duplicate_page" and f["severity"] == "high" and f["data"]["verdict"] == "resubmission"


def test_unextracted_match_or_identical_file_is_left_alone():
    result = {"result": "flag", "details": [_dup(1), _dup(2, identical=True)]}
    assert refine_with_fields(result, _doc_fields("A", "2022-01-01", 1.0), {"m": None}) is result


# --- metadata / ELA ----------------------------------------------------------------------------


def _metadata(data: bytes) -> dict:
    return {f["finding"]: f for f in analyze_pdf_metadata(data)["details"]}


def test_web_page_origin():
    found = _metadata(_born_digital())["web_page_origin"]
    assert found["severity"] == "medium"
    assert found["data"]["engine"] == "PDFium" and found["data"]["css_framework"] == "Bootstrap 5"
    assert "web_page_origin" not in _metadata(_born_digital(producer="Microsoft Word"))


def _with_trailer_orphan(stream_bytes: bytes) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    form = pikepdf.Stream(pdf, stream_bytes)
    form.update({"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Form, "/BBox": [0, 0, 1, 1]})
    pdf.trailer["/Leftover"] = pdf.make_indirect(form)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def test_empty_form_xobject_is_not_an_orphan():
    assert _metadata(_with_trailer_orphan(b""))["orphaned_objects"]["data"]["count"] == 0
    full = _metadata(_with_trailer_orphan(b"q 1 0 0 RG 0 0 m 100 100 l S Q " * 4))["orphaned_objects"]
    assert full["data"]["count"] == 1


def test_ela_not_applicable_says_why():
    result = run_ela_check([SimpleNamespace(has_image_content=False)])
    assert result["result"] == "not_applicable"
    assert result["details"][0]["data"]["reason"] == "born-digital, no images"


def test_a_labelled_trn_in_arabic_digits_or_another_country_counts():
    for text in ("فاتورة ضريبية\nالرقم الضريبي: ٣١٠٢٩١٨٤٠٠٠٠٠٣", "TAX INVOICE\nTRN: 994012841900003"):
        assert validate_fields(_fields(), ocr_text=text)["details"]["tax_invoice_trn"]["status"] == "pass", text
