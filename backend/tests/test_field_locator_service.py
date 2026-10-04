"""app/services/field_locator_service.py — mapping extracted values back onto
OCR word boxes. Pure geometry, so the OCR pages are built by hand."""
import pytest

from app.services.field_locator_service import attach_field_locations, parse_number
from app.services.ocr_service import OCRPage, OCRWord

ROW_H = 0.02


def _page(rows: list[tuple[float, str]], page_number: int = 1) -> OCRPage:
    """Each row is (y, "words separated by spaces"); words run left to right."""
    page = OCRPage(page_number=page_number)
    for y, text in rows:
        x = 0.1
        for token in text.split():
            width = 0.012 * len(token) + 0.01
            page.words.append(OCRWord(token, round(x, 4), y, round(width, 4), ROW_H))
            x += width + 0.01
        page.lines.append(OCRWord(text, 0.1, y, min(0.8, x - 0.1), ROW_H))
    return page


def _fields(**core) -> dict:
    return {"core_fields": {k: dict(v) for k, v in core.items()}, "additional_fields": []}


def _amount(value, raw, currency="USD"):
    return {"value": value, "raw_text": raw, "currency": currency, "confidence": 0.9, "uncertain": False}


@pytest.mark.parametrize(
    "token, expected",
    [("9,030.00", 9030.0), ("1.234,50", 1234.5), ("12,50", 12.5), ("1,234", 1234.0),
     ("٩٬٠٣٠٫٠٠", 9030.0), ("USD", None), ("2026-08-14", None), ("$430", 430.0)],
)
def test_parse_number(token, expected):
    assert parse_number(token) == expected


def test_picks_the_keyword_row_and_gives_each_field_its_own_box():
    page = _page([
        (0.05, "Northwind Office Supplies LLC"),
        (0.12, "Invoice # NW-2026-04417"),
        (0.16, "Issue date: August 14, 2026"),
        (0.40, "Ergonomic chairs 20 240.00 4,800.00 USD"),
        (0.70, "Subtotal: 8,620.00 USD"),
        (0.74, "VAT (5%) 430.00 USD"),
        (0.80, "TOTAL AMOUNT DUE: 9,030.00 USD"),
    ])
    extracted = _fields(
        issuer={"value": "Northwind Office Supplies LLC", "confidence": 0.9, "uncertain": False},
        reference_number={"value": "NW-2026-04417", "confidence": 0.9, "uncertain": False},
        date={"value": "2026-08-14", "raw_text": "August 14, 2026", "confidence": 0.9, "uncertain": False},
        amount=_amount(9030.0, "9,030.00 USD"),
        subtotal=_amount(8620.0, "8,620.00 USD"),
        tax_amount=_amount(430.0, "430.00 USD"),
    )
    located = attach_field_locations([page], extracted)
    core = extracted["core_fields"]

    assert located == 6
    assert core["amount"]["bounding_box"]["y"] == pytest.approx(0.80)
    assert core["subtotal"]["bounding_box"]["y"] == pytest.approx(0.70)
    assert core["tax_amount"]["bounding_box"]["y"] == pytest.approx(0.74)
    assert core["date"]["bounding_box"]["y"] == pytest.approx(0.16)
    assert core["reference_number"]["bounding_box"]["y"] == pytest.approx(0.12)
    assert core["issuer"]["bounding_box"]["y"] == pytest.approx(0.05)
    for field in core.values():
        box = field["bounding_box"]
        assert box["page"] == 1 and 0 <= box["x"] <= 1 and box["width"] > 0 and box["height"] > 0
    # the currency code next to the amount is inside its box
    assert core["amount"]["bounding_box"]["width"] > 0.1


def test_amount_equal_to_subtotal_does_not_share_a_box():
    page = _page([(0.60, "Subtotal 500.00 USD"), (0.70, "Total due 500.00 USD")])
    extracted = _fields(amount=_amount(500.0, "500.00 USD"), subtotal=_amount(500.0, "500.00 USD"))
    attach_field_locations([page], extracted)
    core = extracted["core_fields"]
    assert core["amount"]["bounding_box"]["y"] == pytest.approx(0.70)
    assert core["subtotal"]["bounding_box"]["y"] == pytest.approx(0.60)


def test_arabic_indic_digits_match_the_normalized_value():
    page = _page([(0.5, "الإجمالي ٩٬٠٣٠٫٠٠ AED")])
    extracted = _fields(amount=_amount(9030.0, "9,030.00 AED", "AED"))
    assert attach_field_locations([page], extracted) == 1


def test_mixed_script_issuer_is_found_by_its_arabic_segment():
    page = _page([(0.04, "شركة الأفق الهندسية"), (0.08, "Al Ufuq Engineering Co.")])
    extracted = _fields(
        issuer={"value": "شركة الأفق الهندسية Al Ufuq Engineering Co.", "confidence": 0.9, "uncertain": False}
    )
    assert attach_field_locations([page], extracted) == 1
    assert extracted["core_fields"]["issuer"]["bounding_box"]["y"] == pytest.approx(0.04)


def test_unfindable_and_short_values_get_no_box_and_never_raise():
    page = _page([(0.1, "Some unrelated text 12")])
    extracted = {
        "core_fields": {
            "amount": _amount(777.0, "777.00 USD"),
            "issuer": {"value": None, "confidence": 0.0, "uncertain": False},
        },
        "additional_fields": [
            {"field_name": "qty", "value": "1", "confidence": 0.9, "uncertain": False},
            {"field_name": "note", "value": "unrelated text", "confidence": 0.9, "uncertain": False},
        ],
    }
    assert attach_field_locations([page], extracted) == 1  # only the additional "unrelated text"
    assert "bounding_box" not in extracted["core_fields"]["amount"]
    assert "bounding_box" not in extracted["additional_fields"][0]
    assert attach_field_locations([], extracted) == 0


def test_existing_boxes_are_left_alone():
    page = _page([(0.5, "Total 100.00 USD")])
    keep = {"page": 3, "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}
    extracted = _fields(amount={**_amount(100.0, "100.00 USD"), "bounding_box": keep})
    assert attach_field_locations([page], extracted) == 0
    assert extracted["core_fields"]["amount"]["bounding_box"] == keep
