"""
Fixes from CASE-346FA23E (a fee invoice printed from a school portal, then
saved as a PDF on an iPhone):

  - line items: total rows and installments of another line are not summed;
  - installment_consistency: the installments add up to the line they split;
  - date_sequence: document date -> browser print date -> PDF creation;
  - stamp vs issuer: abbreviations expanded, generic words dropped;
  - orphaned objects: only content (streams, fonts, images, pages,
    annotations) counts.
"""
from __future__ import annotations

import io

import pikepdf

from app.services.field_validation_service import validate_fields
from app.services.forensics.metadata_forensics import analyze_pdf_metadata


def _fields(lines: list[tuple[str, float]], total: float, date: str = "2023-08-17", **extra) -> dict:
    return {
        "core_fields": {
            "amount": {"value": total}, "date": {"value": date},
            "issuer": {"value": extra.pop("issuer", "ABU DHABI INTERNATIONAL SCHOOL MBZ CITY CAMPUS")},
        },
        "line_items": [{"description": d, "line_total": t} for d, t in lines],
        **extra,
    }


CASE_346_LINES = [
    ("Tuition Fees", 34012), ("Picture (Optional)", 115), ("Standardized Testing", 80), ("Uniform Sale", 1052),
    ("School Books Sale", 3970), ("Uniform Discount", -395), ("Term I", 13480), ("Term Il", 10266), ("Term III", 10266),
]


def test_installments_of_another_line_are_not_summed():
    details = validate_fields(_fields(CASE_346_LINES, 34012))["details"]
    subtotal = details["subtotal_line_item_consistency"]
    assert subtotal["status"] == "flag"
    assert subtotal["reason"].startswith("Line items = 38,834.00, stated total 34,012.00, difference 4,822.00")
    assert "line 7 (Term I): 13,480.00 (an installment of Tuition Fees)" in subtotal["excluded_lines"]
    installments = details["installment_consistency"]
    assert installments["status"] == "pass"
    assert installments["reason"] == (
        "Term I 13,480.00 + Term Il 10,266.00 + Term III 10,266.00 = 34,012.00, the Tuition Fees line."
    )


def test_total_rows_are_not_summed():
    lines = [("Tuition", 1000), ("Books", 200), ("Total Fees", 1200)]
    subtotal = validate_fields(_fields(lines, 1200))["details"]["subtotal_line_item_consistency"]
    assert subtotal["status"] == "pass"
    assert "line 3 (Total Fees): 1,200.00 (a total row)" in subtotal["excluded_lines"]


def test_installments_that_nearly_add_up_are_flagged():
    lines = [("Tuition Fees", 34012), ("Term I", 13480), ("Term II", 10266), ("Term III", 9266)]
    installments = validate_fields(_fields(lines, 34012))["details"]["installment_consistency"]
    assert installments["status"] == "flag"
    assert installments["reason"].endswith("= 33,012.00, but Tuition Fees is 34,012.00 (difference 1,000.00).")


def test_terms_that_are_the_charges_themselves_are_summed():
    # Headstart-style: each term IS a charge; nothing they split.
    lines = [("FEE FOR TERM 1", 22000), ("FEE FOR TERM 2", 16500), ("FEE FOR TERM 3", 16600)]
    details = validate_fields(_fields(lines, 55100))["details"]
    assert details["subtotal_line_item_consistency"]["status"] == "pass"
    assert details["installment_consistency"]["status"] == "skipped"


PDF_INFO = {"CreationDate": "2023-08-21T11:03:59+00:00", "ModDate": "2023-08-21T11:03:59+00:00"}


def test_dates_in_order_pass_with_the_sequence():
    fields = _fields([], 0, pdf_info=PDF_INFO)
    sequence = validate_fields(fields, ocr_text="8/18/23, 12:54 PM\nCampusLIVE | Fee Invoice")["details"]["date_sequence"]
    assert sequence == {
        "status": "pass", "reason": "dated 17 Aug 2023 → printed 18 Aug 2023 → PDF created 21 Aug 2023: in order.",
    }


def test_page_printed_before_its_own_date_is_flagged():
    fields = _fields([], 0, date="2023-08-25", pdf_info=PDF_INFO)
    sequence = validate_fields(fields, ocr_text="8/18/23, 12:54 PM\nFee Invoice")["details"]["date_sequence"]
    assert sequence["status"] == "flag"
    assert "printed 18 Aug 2023, before its own date 25 Aug 2023" in sequence["reason"]


def test_no_print_header_skips_date_sequence():
    sequence = validate_fields(_fields([], 0, pdf_info=PDF_INFO), ocr_text="Fee Invoice")["details"]["date_sequence"]
    assert sequence["status"] == "skipped"


def test_day_first_print_date_is_read_when_month_first_is_impossible():
    fields = _fields([], 0, date="2023-08-17", pdf_info=PDF_INFO)
    sequence = validate_fields(fields, ocr_text="18/08/2023, 12:54\nInvoice")["details"]["date_sequence"]
    assert sequence["status"] == "pass" and "printed 18 Aug 2023" in sequence["reason"]


def test_stamp_matches_after_expanding_abbreviations():
    stamps = [{"kind": "stamp", "text": "Abu Dhabi International (Pvt.) Sr\nMohamed Bin Zayed City\nP.O.Box: 13411 Abu Dhabi, U.A.E"}]
    stamp = validate_fields(_fields([], 0), stamps=stamps)["details"]["stamp_issuer_consistency"]
    assert stamp["status"] == "pass"


def test_a_different_organisation_still_does_not_match():
    stamps = [{"kind": "stamp", "text": "Gulf Trading Company LLC\nP.O.Box 1234 Dubai"}]
    stamp = validate_fields(_fields([], 0), stamps=stamps)["details"]["stamp_issuer_consistency"]
    assert stamp["status"] == "flag"


def _pdf_with_orphan(make) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    # Kept in the file by a trailer key the forensic walk (from /Root and
    # /Info) does not follow — pikepdf would drop a truly unreferenced object.
    pdf.trailer["/Leftover"] = pdf.make_indirect(make(pdf))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _orphans(data: bytes) -> dict:
    return next(f for f in analyze_pdf_metadata(data)["details"] if f["finding"] == "orphaned_objects")


def test_a_stray_array_is_not_an_orphaned_object():
    found = _orphans(_pdf_with_orphan(lambda pdf: pikepdf.Array([0, 0, 612, 792])))
    assert found["severity"] == "info" and found["data"]["count"] == 0 and found["data"]["trivial_ignored"] == 1


def _image(pdf: pikepdf.Pdf) -> pikepdf.Stream:
    image = pikepdf.Stream(pdf, bytes(range(64)))  # incompressible: stays over the empty-stream size
    image.update({"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Image, "/Width": 8, "/Height": 8,
                  "/ColorSpace": pikepdf.Name.DeviceGray, "/BitsPerComponent": 8})
    return image


def test_an_orphaned_image_still_counts():
    found = _orphans(_pdf_with_orphan(_image))
    assert found["severity"] == "medium" and found["data"]["count"] == 1 and found["data"]["kinds"] == ["image"]
