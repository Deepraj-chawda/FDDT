"""
Multi-column line-item tables (CASE-D87E2E82, a car dealer quotation):
Price / Discount / Net Price / VAT % / VAT Amount / Total Price. Each column
is summed on its own and compared with its own total row — never one row's
Net Price with another's Total Price.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.field_validation_service import validate_fields
from app.services.line_item_parsing import enrich_extracted_fields, parse_multi_column_tables


def _table(rows: list[list[str]]) -> SimpleNamespace:
    cells = [{"row_index": r, "column_index": c, "content": v} for r, row in enumerate(rows) for c, v in enumerate(row)]
    return SimpleNamespace(row_count=len(rows), column_count=len(rows[0]), cells=cells)


ITEMS = _table([
    ["Description", "Price", "Discount", "Net Price", "VAT4%", "VAT Amount", "Total Price"],
    ["Vehicle price", "131,900,00", "0.00", "131,900.00", "5.00%", "6,595.00", "138,495.00"],
    ["Warranty 5Y", "0.00", "0.00", "0.00", "0.00%", "D.DD", "O.DD"],
    ["Sales Campaign", "-9,892.50", "0.00", "-9,892.50", "D.DDH:", "-494.63", "-10,387.13"],
    ["Ford Executive Program", "-2,000.00", "0.00", "-2,000.00", "0.00%", "-100.00", "-2,100.00"],
])
TOTALS = _table([["Total Net Price", "120,007.50"], ["VAT", "6,000.37"], ["Total Price", "126,007.87"]])


def test_columns_are_read_separately_with_their_totals():
    parsed = parse_multi_column_tables([ITEMS, TOTALS])
    vehicle, warranty, campaign, _ = parsed["line_items"]
    assert vehicle["columns"] == {"price": 131900.0, "discount": 0.0, "net": 131900.0, "vat": 6595.0, "total": 138495.0}
    assert warranty["columns"]["total"] == 0.0  # OCR's "O.DD"
    assert campaign["line_total"] == -9892.5  # the Net Price, not the Total Price
    assert parsed["column_totals"] == {
        "net": {"label": "Total Net Price", "value": 120007.5},
        "vat": {"label": "VAT", "value": 6000.37},
        "total": {"label": "Total Price", "value": 126007.87},
    }


def _fields() -> dict:
    # What the extraction model gave: one row's Net Price, the others' Total Price.
    llm = [
        {"description": "Vehicle price", "line_total": 131900.0},
        {"description": "Sales Campaign", "line_total": -10387.13},
        {"description": "Ford Executive Program", "line_total": -2100.0},
        {"description": "Deposits Paid", "line_total": 2857.14, "counts_toward_total": False},
    ]
    core = {"subtotal": {"value": 120007.5}, "tax_amount": {"value": 6000.37}, "amount": {"value": 126007.87},
            "tax_rate": {"value": 5.0}}
    return {"core_fields": core, "line_items": llm}


def test_each_column_adds_up_to_its_own_total():
    fields = enrich_extracted_fields(_fields(), ocr_tables=[ITEMS, TOTALS])
    assert [i["description"] for i in fields["line_items"]][0] == "Deposits Paid"  # kept, not counted
    sub = validate_fields(fields)["details"]["subtotal_line_item_consistency"]
    assert sub["status"] == "pass"
    assert sub["reason"] == (
        "Each column adds up to its own total: Net Price column 120,007.50 = Total Net Price; "
        "VAT Amount column 6,000.37 = VAT; Total Price column 126,007.87 = Total Price."
    )


def test_a_wrong_column_total_is_flagged_on_its_own():
    totals = _table([["Total Net Price", "120,007.50"], ["VAT", "6,000.37"], ["Total Price", "127,007.87"]])
    fields = enrich_extracted_fields(_fields(), ocr_tables=[ITEMS, totals])
    sub = validate_fields(fields)["details"]["subtotal_line_item_consistency"]
    assert sub["status"] == "flag"
    assert sub["reason"].startswith(
        "A column does not add up to its own total: Total Price column 126,007.87 vs Total Price 127,007.87 "
        "(difference 1,000.00)."
    )
    assert "Correct: Net Price column 120,007.50 = Total Net Price; VAT Amount column 6,000.37 = VAT." in sub["reason"]


def test_a_plain_description_amount_table_is_not_multi_column():
    plain = _table([["Description", "Amount"], ["Tuition", "1,000.00"], ["Books", "200.00"]])
    assert parse_multi_column_tables([plain]) is None
