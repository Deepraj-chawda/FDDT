"""
Unit tests for app/services/field_validation_service.py — pure logic, no
DB/Celery involved (see tests/test_document_checks.py for the task that
wraps this and writes document_checks rows). Core fields use the
normalized shape produced by app/services/llm_service.py: `date` is ISO
8601, `amount`/`subtotal`/`tax_amount` are plain floats.
"""
from app.services.field_validation_service import validate_fields


def _fields(core: dict, additional: list | None = None) -> dict:
    return {"core_fields": core, "additional_fields": additional or []}


def _str(value: str | None) -> dict:
    return {"value": value, "confidence": 0.9, "uncertain": False}


def _date(value: str | None) -> dict:
    return {"value": value, "raw_text": value, "confidence": 0.9, "uncertain": False}


def _amount(value: float | None, currency: str | None = "USD") -> dict:
    return {"value": value, "currency": currency, "raw_text": str(value), "confidence": 0.9, "uncertain": False}


def test_all_pass_when_everything_is_consistent():
    result = validate_fields(
        _fields(
            {
                "issuer": _str("Acme LLC"),
                "date": _date("2020-01-01"),
                "amount": _amount(150.0),
                "subtotal": _amount(150.0),
                "tax_amount": _amount(None),
                "reference_number": _str("INV-2020-001"),
            },
            [
                {"field_name": "item1_total", "value": "100.00", "confidence": 0.9, "uncertain": False},
                {"field_name": "item2_total", "value": "50.00", "confidence": 0.9, "uncertain": False},
            ],
        )
    )
    assert result["result"] == "pass"
    assert result["details"]["date_in_future"]["status"] == "pass"
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "pass"
    assert result["details"]["total_tax_consistency"]["status"] == "pass"
    assert result["details"]["reference_number_format"]["status"] == "pass"


def test_future_date_is_flagged():
    result = validate_fields(_fields({"date": _date("2099-01-01")}))
    assert result["result"] == "flag"
    assert result["details"]["date_in_future"]["status"] == "flag"


def test_past_date_passes():
    result = validate_fields(_fields({"date": _date("2020-06-15")}))
    assert result["details"]["date_in_future"]["status"] == "pass"


def test_non_iso_date_still_parses_via_fallback():
    # New extractions always produce ISO 8601 — this exercises the
    # fallback path for older/malformed data.
    result = validate_fields(_fields({"date": _date("June 15, 2020")}))
    assert result["details"]["date_in_future"]["status"] == "pass"


def test_missing_date_is_skipped_not_flagged():
    result = validate_fields(_fields({"date": _date(None)}))
    assert result["details"]["date_in_future"]["status"] == "skipped"
    assert result["result"] == "pass"


def test_unparsable_date_is_skipped_not_flagged():
    result = validate_fields(_fields({"date": _date("not a date")}))
    assert result["details"]["date_in_future"]["status"] == "skipped"


def test_subtotal_line_item_mismatch_is_flagged():
    result = validate_fields(
        _fields(
            {"subtotal": _amount(500.0)},
            [
                {"field_name": "item1_total", "value": "100.00", "confidence": 0.9, "uncertain": False},
                {"field_name": "item2_total", "value": "50.00", "confidence": 0.9, "uncertain": False},
            ],
        )
    )
    assert result["result"] == "flag"
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "flag"


def test_subtotal_line_item_within_rounding_tolerance_passes():
    result = validate_fields(
        _fields(
            {"subtotal": _amount(150.0)},
            [{"field_name": "item1_total", "value": "149.99", "confidence": 0.9, "uncertain": False}],
        )
    )
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "pass"


def test_no_line_items_is_skipped_not_flagged():
    result = validate_fields(_fields({"subtotal": _amount(150.0)}))
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "skipped"
    assert result["result"] == "pass"


def test_no_subtotal_is_skipped_even_with_line_items():
    result = validate_fields(
        _fields(
            {"amount": _amount(150.0)},
            [{"field_name": "item1_total", "value": "150.00", "confidence": 0.9, "uncertain": False}],
        )
    )
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "skipped"


def test_unrelated_additional_fields_do_not_count_as_line_items():
    result = validate_fields(
        _fields(
            {"subtotal": _amount(150.0)},
            [{"field_name": "vendor_address", "value": "123 Main St", "confidence": 0.9, "uncertain": False}],
        )
    )
    assert result["details"]["subtotal_line_item_consistency"]["status"] == "skipped"


def test_total_equals_subtotal_plus_tax_passes():
    result = validate_fields(
        _fields({"amount": _amount(115.0), "subtotal": _amount(100.0), "tax_amount": _amount(15.0)})
    )
    assert result["details"]["total_tax_consistency"]["status"] == "pass"


def test_total_tax_mismatch_is_flagged():
    result = validate_fields(
        _fields({"amount": _amount(200.0), "subtotal": _amount(100.0), "tax_amount": _amount(15.0)})
    )
    assert result["result"] == "flag"
    assert result["details"]["total_tax_consistency"]["status"] == "flag"


def test_no_tax_shown_validates_total_equals_subtotal_not_flagged_for_missing_tax():
    result = validate_fields(_fields({"amount": _amount(100.0), "subtotal": _amount(100.0)}))
    assert result["details"]["total_tax_consistency"]["status"] == "pass"


def test_no_tax_shown_but_total_differs_from_subtotal_is_flagged():
    result = validate_fields(_fields({"amount": _amount(150.0), "subtotal": _amount(100.0)}))
    assert result["details"]["total_tax_consistency"]["status"] == "flag"


def test_total_tax_consistency_skipped_without_subtotal():
    result = validate_fields(_fields({"amount": _amount(150.0)}))
    assert result["details"]["total_tax_consistency"]["status"] == "skipped"
    assert result["result"] == "pass"


def test_malformed_reference_number_is_flagged():
    result = validate_fields(_fields({"reference_number": _str("!!! ???")}))
    assert result["result"] == "flag"
    assert result["details"]["reference_number_format"]["status"] == "flag"


def test_reference_number_without_digit_is_flagged():
    result = validate_fields(_fields({"reference_number": _str("INVOICE")}))
    assert result["details"]["reference_number_format"]["status"] == "flag"


def test_valid_reference_number_passes():
    result = validate_fields(_fields({"reference_number": _str("INV-ENEC-2026-1049")}))
    assert result["details"]["reference_number_format"]["status"] == "pass"


def test_missing_reference_number_is_skipped_not_flagged():
    result = validate_fields(_fields({"reference_number": _str(None)}))
    assert result["details"]["reference_number_format"]["status"] == "skipped"


def test_empty_extracted_fields_all_skipped():
    result = validate_fields({})
    assert result["result"] == "pass"
    assert all(sub["status"] == "skipped" for sub in result["details"].values())
