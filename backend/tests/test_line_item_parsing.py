"""Line items read from the OCR layout (app/services/line_item_parsing.py)."""
import pytest

from app.services.line_item_parsing import (
    enrich_extracted_fields,
    parse_rate_quantity_lines,
    parse_single_row_tables,
)
from app.services.ocr_service import OCRTable

HEADSTART_TEXT = """HEADSTART NURSERY
INVOICE
1. REGISTRATION FEES: 500 PAID
2. FEE FOR TERM 1 (September) PAID (October-November-December) DUE NUMBER OF DAYS PER WEEK: 5 DAYS
FEE:
5,500 PER MONTH X 4 MONTH = 22,000
3. FEE FOR TERM 2 (January-February-March) DUE
NUMBER OF DAYS PER WEEK: 5 DAYS FEE: 5,500 PER MONTH X 3 MONTHS = 16,500
4. FEE FOR TERM 3 (April-May-June) DUE
NUMBER OF DAYS PER WEEK: 5 DAYS
FEE: 6,000 PER MONTH X 3 MONTHS = 16,600
TOTAL FEE FOR TERM 1, 2 AND TERM 3 (FOR THE ACADEMIC YEAR 2022-2023) IS: 55,500
"""


def _cells(rows):
    return [
        {"row_index": r, "column_index": c, "content": value}
        for r, row in enumerate(rows)
        for c, value in enumerate(row)
    ]


GIPA_TABLE = OCRTable(
    row_count=2,
    column_count=9,
    cells=_cells(
        [
            ["Name", "Grade", "Tuition Fees", "Books", "Uniform", "Technology Fee", "Transport", "5% Vat", "Total Fees"],
            ["GHAYA AHMED SALEM ABDULLA ALKINDI", "KG2", "52,130", "650", "300", "550", "3,510", "42.50", "57,182.50"],
        ]
    ),
)


@pytest.mark.parametrize(
    "line, rate, qty, unit, total",
    [
        ("5,500 PER MONTH X 4 MONTH = 22,000", 5500, 4, "MONTH", 22000),
        ("5,500 PER MONTH X 3 MONTHS = 16,500", 5500, 3, "MONTH", 16500),
        ("6,000 per month x 3 months = 16,600", 6000, 3, "MONTH", 16600),
        ("1,250.50 PER WEEK × 2 WEEKS = 2,501.00", 1250.5, 2, "WEEK", 2501),
        ("800 PER MONTHS X3 MONTHS=2,400", 800, 3, "MONTH", 2400),
        ("2, 400 PER TERM X 1 TERM = 2, 400", 2400, 1, "TERM", 2400),
    ],
)
def test_rate_quantity_line_forms(line, rate, qty, unit, total):
    [item] = parse_rate_quantity_lines(line)
    assert (item["unit_price"], item["quantity"], item["unit"], item["line_total"]) == (rate, qty, unit, total)
    assert item["source"] == "text_pattern"


def test_rate_quantity_ignores_rates_without_a_multiplier():
    assert parse_rate_quantity_lines("NUMBER OF DAYS PER WEEK: 5 DAYS\nLate payments accrue 1.5% per month.") == []


def test_headstart_lines_status_and_periods():
    items = parse_rate_quantity_lines(HEADSTART_TEXT)
    assert [i["line_total"] for i in items] == [22000, 16500, 16600]
    term1, term2, term3 = items
    # September PAID, October-December DUE: the line is payable, not paid.
    assert term1["status"] == "due" and term1["counts_toward_total"] is True
    assert term1["paid_periods"] == ["September"]
    assert term1["due_periods"] == ["October", "November", "December"]
    assert term1["description"] == "FEE FOR TERM 1 (September) PAID (October-November-December) DUE"
    assert term2["due_periods"] == ["January", "February", "March"]
    assert term3["description"] == "FEE FOR TERM 3 (April-May-June) DUE"
    assert term3["quantity"] == 3 and term3["unit_price"] == 6000


def test_line_marked_only_paid_is_kept_but_not_counted():
    [item] = parse_rate_quantity_lines("1. TERM 1 FEE (September) PAID\n2,000 PER MONTH X 1 MONTH = 2,000")
    assert item["status"] == "paid"
    assert item["counts_toward_total"] is False


def test_single_row_table_columns_become_line_items():
    parsed = parse_single_row_tables([GIPA_TABLE])
    assert [(i["description"], i["line_total"]) for i in parsed["line_items"]] == [
        ("Tuition Fees", 52130), ("Books", 650), ("Uniform", 300), ("Technology Fee", 550), ("Transport", 3510),
    ]
    assert parsed["tax_amount"] == (42.5, "42.50")
    assert parsed["tax_rate"] == (5.0, "5% Vat")
    assert parsed["total"] == (57182.5, "57,182.50")


@pytest.mark.parametrize(
    "rows",
    [
        [["Description", "Amount"], ["Consulting services", "12,500.00"]],  # an item list with one line
        [["Subtotal:", "6,960.00 USD"], ["TOTAL AMOUNT DUE:", "6,960.00 USD"]],  # label/value pairs
        [["Tuition", "Books"], ["1,000", "200"]],  # no total column
    ],
)
def test_other_two_row_tables_are_not_read_as_charges(rows):
    assert parse_single_row_tables([OCRTable(row_count=2, column_count=len(rows[0]), cells=_cells(rows))]) is None


def test_enrich_replaces_matching_llm_lines_and_marks_paid_registration():
    llm_items = [
        {"description": "REGISTRATION FEES", "quantity": None, "unit_price": 500.0, "line_total": 500.0,
         "line_total_raw_text": "500", "counts_toward_total": False, "discount": None, "confidence": 0.9,
         "uncertain": False},
        # The model dropped term 1 from the payable sum.
        {"description": "FEE FOR TERM 1 (September)", "quantity": 4.0, "unit_price": 5500.0, "line_total": 22000.0,
         "line_total_raw_text": "22,000", "counts_toward_total": False, "discount": None, "confidence": 0.95,
         "uncertain": False, "bounding_box": {"page": 1, "x": 0.43, "y": 0.43, "width": 0.05, "height": 0.01}},
    ]
    fields = enrich_extracted_fields({"line_items": llm_items, "core_fields": {}}, ocr_text=HEADSTART_TEXT)
    items = fields["line_items"]
    assert [i["line_total"] for i in items] == [500, 22000, 16500, 16600]
    assert items[0]["source"] == "llm" and items[0]["status"] == "paid" and items[0]["counts_toward_total"] is False
    assert items[1]["source"] == "text_pattern" and items[1]["counts_toward_total"] is True
    assert items[1]["bounding_box"]["y"] == 0.43  # located box kept
    # The input is not modified.
    assert llm_items[1]["counts_toward_total"] is False


def test_enrich_fills_only_empty_core_fields_from_a_table():
    core = {"amount": {"value": 57182.5, "raw_text": "57,182.50", "currency": "AED"}, "tax_amount": {"value": None}}
    fields = enrich_extracted_fields({"core_fields": core, "line_items": []}, ocr_tables=[GIPA_TABLE])
    assert fields["core_fields"]["amount"]["currency"] == "AED"
    assert fields["core_fields"]["tax_amount"]["value"] == 42.5
    assert fields["core_fields"]["tax_rate"]["value"] == 5.0
    assert len(fields["line_items"]) == 5


def test_enrich_leaves_unrecognised_documents_alone():
    llm_items = [{"description": "Widget", "line_total": 10.0, "counts_toward_total": True}]
    fields = enrich_extracted_fields({"line_items": llm_items}, ocr_text="Invoice\nWidget 10.00\nTotal 10.00")
    assert fields["line_items"] == [dict(llm_items[0], source="llm")]
    assert "pdf_info" not in fields
