"""
Unit tests for the arithmetic sub-checks in app/services/field_validation_service.py
(line_item_arithmetic, the structured-line-item path of
subtotal_line_item_consistency, tax_rate_consistency,
amount_in_words_consistency), the English amount-words reader
(app/services/amount_words.py), and locating line items on the page
(app/services/field_locator_service.py). Pure logic, no DB.
"""
import pytest

from app.services.amount_words import parse_english_amount
from app.services.field_locator_service import attach_field_locations
from app.services.field_validation_service import validate_fields
from app.services.llm_service import _analysis_json_schema
from app.services.ocr_service import OCRPage, OCRWord


def _amount(value: float | None, currency: str | None = "AED", box: dict | None = None) -> dict:
    field = {"value": value, "currency": currency, "raw_text": None if value is None else f"{value:,.2f}",
             "confidence": 0.9, "uncertain": False}
    if box:
        field["bounding_box"] = box
    return field


def _rate(value: float) -> dict:
    return {"value": value, "raw_text": f"{value:g}%", "confidence": 0.9, "uncertain": False}


def _item(total, quantity=None, unit_price=None, *, discount=None, counts=True, description="Item", box=None) -> dict:
    item = {"description": description, "quantity": quantity, "unit_price": unit_price, "discount": discount,
            "line_total": total, "line_total_raw_text": None, "counts_toward_total": counts,
            "confidence": 0.9, "uncertain": False}
    if box:
        item["bounding_box"] = box
    return item


def _words(text: str | None, value: float | None = None, box: dict | None = None) -> dict:
    words = {"text": text, "value": value, "confidence": 0.9}
    if box:
        words["bounding_box"] = box
    return words


def _validate(core: dict, items: list | None = None, words: dict | None = None) -> dict:
    return validate_fields({"core_fields": core, "additional_fields": [], "line_items": items or [],
                            "amount_in_words": words})["details"]


BOX = {"page": 1, "x": 0.4, "y": 0.5, "width": 0.05, "height": 0.01}

# The scanned nursery invoice (sample_font20): three separate calculation errors.
NURSERY = dict(
    core={"amount": _amount(55500.0, box=BOX)},
    items=[
        _item(500, counts=False, description="Registration fees (paid)"),
        _item(22000, 4, 5500, description="Term 1"),
        _item(16500, 3, 5500, description="Term 2"),
        _item(16600, 3, 6000, description="Term 3", box=BOX),
    ],
    words=_words("TWENTY THOUSAND FIVE HUNDRED AED ONLY", 20500, box=BOX),
)


def test_nursery_invoice_flags_all_three_calculation_errors():
    details = _validate(NURSERY["core"], NURSERY["items"], NURSERY["words"])
    arithmetic = details["line_item_arithmetic"]
    assert arithmetic["status"] == "flag"
    assert "3 × 6,000.00 = 18,000.00" in arithmetic["reason"] and "16,600.00" in arithmetic["reason"]
    assert [r["field"] for r in arithmetic["regions"]] == ["line_item_4"]

    total = details["subtotal_line_item_consistency"]
    assert total["status"] == "flag"
    assert "55,100.00" in total["reason"] and "55,500.00" in total["reason"] and "400.00" in total["reason"]
    assert [r["field"] for r in total["regions"]] == ["amount"]

    words = details["amount_in_words_consistency"]
    assert words["status"] == "flag"
    assert "20,500.00" in words["reason"] and "55,500.00" in words["reason"]
    assert [r["field"] for r in words["regions"]] == ["amount", "amount_in_words"]


# --- line item arithmetic -------------------------------------------------------


def test_correct_lines_pass_including_discount_and_cent_rounding():
    details = _validate({}, [_item(300.0, 3, 100), _item(90.0, 1, 100, discount=10), _item(10.0, 3, 3.333)])
    assert details["line_item_arithmetic"]["status"] == "pass"


def test_lines_without_quantity_or_price_are_skipped():
    assert _validate({}, [_item(500.0), _item(None, 2, 10)])["line_item_arithmetic"]["status"] == "skipped"


def test_a_line_off_by_more_than_rounding_is_flagged():
    assert _validate({}, [_item(300.05, 3, 100)])["line_item_arithmetic"]["status"] == "flag"


# --- line items vs subtotal / total ------------------------------------------------


def test_lines_are_compared_to_the_subtotal_when_one_is_shown():
    core = {"subtotal": _amount(400.0), "amount": _amount(420.0), "tax_amount": _amount(20.0)}
    assert _validate(core, [_item(100.0), _item(300.0)])["subtotal_line_item_consistency"]["status"] == "pass"
    assert _validate(core, [_item(100.0), _item(310.0)])["subtotal_line_item_consistency"]["status"] == "flag"


def test_without_subtotal_lines_are_compared_to_total_less_tax():
    core = {"amount": _amount(420.0), "tax_amount": _amount(20.0)}
    assert _validate(core, [_item(100.0), _item(300.0)])["subtotal_line_item_consistency"]["status"] == "pass"


def test_a_one_percent_gap_is_no_longer_tolerated():
    core = {"amount": _amount(10000.0)}
    assert _validate(core, [_item(9950.0)])["subtotal_line_item_consistency"]["status"] == "flag"


def test_excluded_lines_that_actually_count_do_not_cause_a_false_flag():
    # The model marked the deposit as excluded, but including it is what adds up.
    core = {"amount": _amount(600.0)}
    items = [_item(100.0, counts=False), _item(500.0)]
    assert _validate(core, items)["subtotal_line_item_consistency"]["status"] == "pass"


def test_sum_tolerance_is_one_cent_per_summed_line_not_a_percentage():
    items = [_item(100.01), _item(200.01), _item(300.01)]
    # 3 lines may drift 3 cents by rounding...
    assert _validate({"amount": _amount(600.0)}, items)["subtotal_line_item_consistency"]["status"] == "pass"
    # ...but not 4, and a single line not even 2.
    assert _validate({"amount": _amount(599.99)}, items)["subtotal_line_item_consistency"]["status"] == "flag"
    assert _validate({"amount": _amount(100.0)}, [_item(100.02)])["subtotal_line_item_consistency"]["status"] == "flag"


def test_headstart_sum_mismatch_wording_and_summed_lines():
    # CASE-DB43653A after line-item parsing: term 1 counts, registration is paid.
    items = [
        dict(_item(500, counts=False, description="REGISTRATION FEES"), status="paid"),
        dict(_item(22000, 4, 5500, description="FEE FOR TERM 1"), status="due"),
        dict(_item(16500, 3, 5500, description="FEE FOR TERM 2"), status="due"),
        dict(_item(16600, 3, 6000, description="FEE FOR TERM 3"), status="due"),
    ]
    sub = _validate({"amount": _amount(55500.0)}, items)["subtotal_line_item_consistency"]
    assert sub["status"] == "flag"
    assert sub["reason"].startswith("Line items = 55,100.00, stated total 55,500.00, difference 400.00")
    assert "line 1 (REGISTRATION FEES): 500.00 (marked paid)" in sub["reason"]
    assert sub["summed_lines"] == [
        "line 2 (FEE FOR TERM 1): 22,000.00", "line 3 (FEE FOR TERM 2): 16,500.00", "line 4 (FEE FOR TERM 3): 16,600.00",
    ]
    assert sub["excluded_lines"] == ["line 1 (REGISTRATION FEES): 500.00"]


def test_a_line_marked_paid_is_never_summed_by_the_fallback():
    # Summing the paid line too would match, but the document says it is paid.
    items = [dict(_item(100.0, counts=False), status="paid"), _item(500.0)]
    assert _validate({"amount": _amount(600.0)}, items)["subtotal_line_item_consistency"]["status"] == "flag"


def test_legacy_additional_field_line_items_still_work():
    fields = {"core_fields": {"subtotal": _amount(150.0)},
              "additional_fields": [{"field_name": "item1_total", "value": "100.00"},
                                    {"field_name": "item2_total", "value": "40.00"}]}
    assert validate_fields(fields)["details"]["subtotal_line_item_consistency"]["status"] == "flag"


# --- tax rate -----------------------------------------------------------------------


def test_tax_matching_the_rate_passes_and_a_wrong_one_is_flagged():
    core = {"subtotal": _amount(1000.0), "tax_rate": _rate(5), "tax_amount": _amount(50.0)}
    assert _validate(core)["tax_rate_consistency"]["status"] == "pass"
    core["tax_amount"] = _amount(70.0)
    result = _validate(core)["tax_rate_consistency"]
    assert result["status"] == "flag" and "expected 50.00" in result["reason"]


def test_tax_rate_without_subtotal_uses_total_less_tax():
    core = {"amount": _amount(1050.0), "tax_rate": _rate(5), "tax_amount": _amount(50.0)}
    assert _validate(core)["tax_rate_consistency"]["status"] == "pass"


GIPA_ITEMS = [
    _item(52130.0, description="Tuition Fees"), _item(650.0, description="Books"), _item(300.0, description="Uniform"),
    _item(550.0, description="Technology Fee"), _item(3510.0, description="Transport"),
]


def test_mixed_rate_vat_on_a_subset_of_lines_passes_and_names_it():
    # CASE-DE627FA2: 5% VAT on uniform + technology fee only (850), 42.50.
    core = {"amount": _amount(57182.5), "tax_rate": _rate(5), "tax_amount": _amount(42.5)}
    result = _validate(core, GIPA_ITEMS)["tax_rate_consistency"]
    assert result["status"] == "pass"
    assert result["reason"].startswith("Tax applies to {Uniform, Technology Fee} = 850.00")
    assert result["taxed_lines"] == ["line 3 (Uniform)", "line 4 (Technology Fee)"]


def test_tax_no_subset_explains_is_still_flagged():
    core = {"amount": _amount(57183.0), "tax_rate": _rate(5), "tax_amount": _amount(43.0)}
    result = _validate(core, GIPA_ITEMS)["tax_rate_consistency"]
    assert result["status"] == "flag"
    assert "No combination of the line items explains it" in result["reason"]


def test_subset_tolerance_is_one_cent_per_taxed_line():
    core = {"amount": _amount(1000.0), "tax_rate": _rate(5), "tax_amount": _amount(10.02)}
    items = [_item(200.0, description="A"), _item(780.0, description="B")]
    # 5% of 200 = 10.00: 2 cents off one taxed line is more than rounding.
    assert _validate(core, items)["tax_rate_consistency"]["status"] == "flag"
    core["tax_amount"] = _amount(10.01)
    assert _validate(core, items)["tax_rate_consistency"]["status"] == "pass"


def test_paid_lines_are_not_a_taxed_base():
    core = {"amount": _amount(1015.0), "tax_rate": _rate(5), "tax_amount": _amount(15.0)}
    items = [dict(_item(300.0, counts=False, description="Deposit"), status="paid"), _item(700.0, description="Fee")]
    assert _validate(core, items)["tax_rate_consistency"]["status"] == "flag"


def test_more_than_twenty_lines_only_tries_the_whole_subtotal():
    core = {"amount": _amount(2105.0), "tax_rate": _rate(5), "tax_amount": _amount(5.0)}
    items = [_item(100.0, description=f"L{i}") for i in range(21)]
    result = _validate(core, items)["tax_rate_consistency"]
    assert result["status"] == "flag" and "No combination" not in result["reason"]


# --- amount in words --------------------------------------------------------------


def test_words_matching_the_total_pass_even_without_the_fils():
    core = {"amount": _amount(55500.5)}
    words = _words("Fifty-Five Thousand Five Hundred Dirhams Only", 55500)
    assert _validate(core, words=words)["amount_in_words_consistency"]["status"] == "pass"


def test_english_words_are_read_here_not_taken_from_the_model():
    # The model "corrected" the words to the total; the words themselves say otherwise.
    core = {"amount": _amount(55500.0)}
    result = _validate(core, words=_words("Twenty Thousand Five Hundred AED Only", 55500))["amount_in_words_consistency"]
    assert result["status"] == "flag" and "read from the words" in result["reason"]


def test_other_languages_fall_back_to_the_models_reading():
    core = {"amount": _amount(5000.0)}
    words = _words("فقط خمسة آلاف درهم", 5000)
    assert _validate(core, words=words)["amount_in_words_consistency"]["status"] == "pass"
    words = _words("فقط ستة آلاف درهم", 6000)
    result = _validate(core, words=words)["amount_in_words_consistency"]
    assert result["status"] == "flag" and "extraction model" in result["reason"]


def test_words_may_state_the_subtotal():
    core = {"subtotal": _amount(1000.0), "amount": _amount(1050.0)}
    assert _validate(core, words=_words("One Thousand Dirhams Only"))["amount_in_words_consistency"]["status"] == "pass"


def test_no_words_is_skipped():
    assert _validate({"amount": _amount(1.0)}, words=_words(None))["amount_in_words_consistency"]["status"] == "skipped"
    assert _validate({"amount": _amount(1.0)})["amount_in_words_consistency"]["status"] == "skipped"


@pytest.mark.parametrize(
    "text, value",
    [
        ("TWENTY THOUSAND FIVE HUNDRED AED ONLY", 20500),
        ("Fifty-Five Thousand Five Hundred Dirhams Only", 55500),
        ("one hundred and five dollars and fifty cents", 105.5),
        ("Twelve hundred dollars", 1200),
        ("One Million Two Hundred Thousand", 1_200_000),
        ("Five Hundred Dirhams and Two Hundred Fifty Fils", 500.25),
        ("AED 55,500 only", None),
        ("فقط خمسة آلاف درهم", None),
        ("Payment overdue", None),
    ],
)
def test_parse_english_amount(text, value):
    assert parse_english_amount(text) == value


# --- extraction schema and page locations ------------------------------------------


def test_extraction_schema_requires_line_items_and_amount_in_words():
    # Azure OpenAI's strict JSON mode needs every property listed as required.
    schema = _analysis_json_schema()
    assert {"line_items", "amount_in_words"} <= set(schema["required"])
    item = schema["properties"]["line_items"]["items"]
    assert set(item["required"]) == set(item["properties"])


def _word(text: str, x: float, y: float) -> OCRWord:
    return OCRWord(text, x, y, 0.04, 0.012)


def test_line_totals_are_located_on_their_own_lines():
    # The same total (16,500) is printed on two lines: each item gets its own.
    page = OCRPage(page_number=1, words=[
        _word("Term", 0.1, 0.30), _word("1", 0.15, 0.30), _word("5,500", 0.25, 0.30), _word("16,500", 0.5, 0.30),
        _word("Term", 0.1, 0.40), _word("2", 0.15, 0.40), _word("5,500", 0.25, 0.40), _word("16,500", 0.5, 0.40),
        _word("TWENTY", 0.1, 0.6), _word("THOUSAND", 0.2, 0.6), _word("ONLY", 0.3, 0.6),
    ])
    page.lines = [OCRWord("TWENTY THOUSAND ONLY", 0.1, 0.6, 0.3, 0.012)]
    fields = {"core_fields": {}, "additional_fields": [],
              "line_items": [_item(16500, 3, 5500, description="Term 1"), _item(16500, 3, 5500, description="Term 2")],
              "amount_in_words": _words("TWENTY THOUSAND ONLY", 20000)}
    attach_field_locations([page], fields)
    assert [i["bounding_box"]["y"] for i in fields["line_items"]] == [0.3, 0.4]
    assert fields["amount_in_words"]["bounding_box"]["y"] == 0.6
