"""
Unit tests for app/services/forensics/font_consistency.py — pure logic over
raw PDF bytes (and, for scanned pages, OCR words with font estimates). Pages
are built in memory with PyMuPDF's built-in base-14 fonts (helv/tiro/cour and
their bold variants), so no font files are needed.
"""
from __future__ import annotations

import pymupdf
import pytest

from app.services.forensics.font_consistency import (
    _script,
    analyze_font_consistency,
    font_family,
    ocr_font_class,
)
from app.services.ocr_service import OCRPage, OCRWord

# PyMuPDF base-14 font codes
HELV, HELV_BOLD, HELV_ITALIC = "helv", "hebo", "heit"
TIMES, TIMES_BOLD = "tiro", "tibo"
COURIER = "cour"


def _invoice(overrides: dict[tuple[int, int], tuple[str, float | None]] | None = None, *, size: float = 9) -> bytes:
    """A one-page invoice laid out as a table: a heading, a header row, four
    item rows and two summary rows. `overrides` maps (row, column) to
    (font, size or None) for the cells to set differently."""
    overrides = overrides or {}
    rows = [
        ["#", "Description", "Qty", "Unit price", "Total"],
        ["1", "Laptop 16GB", "10", "1,150.00", "11,500.00"],
        ["2", "Docking station", "10", "185.00", "1,850.00"],
        ["3", "Monitor 27 inch", "10", "210.00", "2,100.00"],
        ["4", "Keyboard and mouse", "10", "45.00", "450.00"],
    ]
    xs = [50, 80, 300, 360, 450]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "TECHSOURCE SOLUTIONS INC.", fontname=HELV_BOLD, fontsize=16)
    page.insert_text((50, 80), "Enterprise IT procurement and corporate accounts", fontname=HELV, fontsize=size)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            font, cell_size = overrides.get((r, c), (HELV_BOLD if r == 0 else HELV, None))
            page.insert_text((xs[c], 120 + r * 18), text, fontname=font, fontsize=cell_size or size)
    for i, (label, value) in enumerate([("Subtotal:", "15,900.00 USD"), ("Sales tax (7%):", "1,113.00 USD")]):
        r = len(rows) + i
        label_font, label_size = overrides.get((r, 0), (HELV, None))
        value_font, value_size = overrides.get((r, 1), (HELV, None))
        page.insert_text((300, 120 + r * 18 + 10), label, fontname=label_font, fontsize=label_size or size)
        page.insert_text((450, 120 + r * 18 + 10), value, fontname=value_font, fontsize=value_size or size)
    page.insert_text((50, 300), "Payment due within 30 days of the invoice date.", fontname=HELV, fontsize=size)
    data = doc.tobytes()
    doc.close()
    return data


def _font_hits(pdf: bytes, ocr_pages: list[OCRPage] | None = None) -> list[dict]:
    result = analyze_font_consistency(pdf, ocr_pages)
    return [f for f in result["details"] if f["finding"] == "font_inconsistency"]


# --- font family normalization ------------------------------------------------


@pytest.mark.parametrize(
    "name, family",
    [
        ("ABCDEF+Arial-BoldMT", "helvetica"),
        ("Helvetica-Oblique", "helvetica"),
        ("TimesNewRomanPS-BoldItalicMT", "times"),
        ("Times-Roman", "times"),
        ("CourierNewPSMT", "courier"),
        ("Liberation-Mono-Bold", "courier"),
        ("Calibri,Bold", "calibri"),
        ("DejaVu-Sans-Bold", "dejavusans"),
        ("DejaVuSansMono", "dejavusansmono"),
    ],
)
def test_font_family_ignores_style_subset_and_metric_aliases(name, family):
    assert font_family(name) == family


def test_script_separates_writing_systems():
    assert _script("Invoice 1,250.00") == "latin"
    assert _script("1,250.00") == "latin"
    assert _script("رقم الفاتورة") == "arabic"


# --- clean documents ------------------------------------------------------------


def test_single_family_document_is_clean_and_inventoried():
    result = analyze_font_consistency(_invoice())
    assert result["result"] == "pass"
    (inventory,) = result["details"]
    assert inventory["finding"] == "font_family_inventory" and inventory["severity"] == "info"
    assert inventory["data"]["families"].keys() == {"helvetica"}
    assert inventory["data"]["pages"] == [{"page": 1, "source": "text_layer"}]


def test_bold_and_italic_of_the_same_family_are_not_a_mismatch():
    pdf = _invoice({(1, 4): (HELV_BOLD, None), (2, 1): (HELV_ITALIC, None), (5, 1): (HELV_BOLD, None)})
    assert _font_hits(pdf) == []


def test_a_heading_in_another_family_at_another_size_is_design_not_an_edit():
    doc = pymupdf.open(stream=_invoice(), filetype="pdf")
    doc[0].insert_text((350, 60), "TAX INVOICE", fontname=TIMES_BOLD, fontsize=20)
    assert _font_hits(doc.tobytes()) == []


def test_a_family_used_for_a_large_share_of_the_page_is_design():
    # Every description is set in Courier: a deliberate design choice.
    pdf = _invoice({(r, 1): (COURIER, None) for r in range(1, 5)} | {(0, 1): (COURIER, None)})
    assert _font_hits(pdf) == []


def test_scan_without_ocr_fonts_is_reported_not_flagged():
    result = analyze_font_consistency(_blank_scan())
    assert result["result"] == "pass"
    assert [f["finding"] for f in result["details"]] == ["font_family_inventory"]
    assert "No text layer or OCR font information" in result["details"][0]["description"]
    assert result["details"][0]["data"]["pages"] == [{"page": 1, "source": "none"}]


# --- edits ----------------------------------------------------------------------


def test_amount_retyped_in_another_family_is_flagged_with_its_box():
    (hit,) = _font_hits(_invoice({(1, 3): (TIMES, None)}))
    assert hit["severity"] == "high" and hit["data"]["source"] == "text_layer"
    assert hit["data"]["text"] == "1,150.00"
    assert hit["data"]["family"] == "times" and hit["data"]["expected_family"] == "helvetica"
    assert "1,150.00" in hit["description"] and "Times-Roman" in hit["description"]
    box = hit["bounding_box"]
    assert hit["page"] == box["page"] == 1
    assert 0 < box["x"] < 1 and 0 < box["y"] < 1 and 0 < box["width"] < 0.2 and 0 < box["height"] < 0.05


@pytest.mark.parametrize("size", [4, 6, 9, 14])
def test_mismatch_is_flagged_at_any_font_size(size):
    hits = _font_hits(_invoice({(3, 4): (TIMES, None)}, size=size))
    assert [h["data"]["text"] for h in hits] == ["2,100.00"]


def test_single_digit_edit_is_flagged():
    hits = _font_hits(_invoice({(2, 2): (COURIER, None)}))
    assert [h["data"]["text"] for h in hits] == ["10"]


def test_summary_value_is_flagged_not_its_label():
    # One label + one value on the row: the value's font is the rare one on
    # the page, so the value is the outlier.
    hits = _font_hits(_invoice({(5, 1): (TIMES, None)}))
    assert [h["data"]["text"] for h in hits] == ["15,900.00 USD"]


def test_a_whole_row_retyped_is_caught_by_its_columns():
    pdf = _invoice({(2, c): (COURIER, None) for c in range(5)})
    assert {h["data"]["text"] for h in _font_hits(pdf)} == {"2", "Docking station", "10", "185.00", "1,850.00"}


def test_several_edits_each_get_a_finding():
    pdf = _invoice({(1, 3): (TIMES, None), (1, 4): (TIMES, None), (5, 1): (TIMES, None), (6, 1): (TIMES_BOLD, None)})
    assert [h["data"]["text"] for h in _font_hits(pdf)] == ["1,150.00", "11,500.00", "15,900.00 USD", "1,113.00 USD"]


def test_metric_compatible_substitute_is_not_flagged():
    # Arial == Helvetica for this check; only the family matters.
    doc = pymupdf.open(stream=_invoice(), filetype="pdf")
    page = doc[0]
    rect = page.search_for("185.00")[0]
    page.add_redact_annot(rect)
    page.apply_redactions()
    page.insert_font(fontname="F_arial", fontbuffer=pymupdf.Font("helv").buffer)
    page.insert_text((rect.x0, rect.y1 - 2), "185.00", fontname="F_arial", fontsize=9)
    assert _font_hits(doc.tobytes()) == []


# --- scanned pages: OCR font estimates ------------------------------------------

BODY = "Segoe UI, Tahoma, sans-serif"
BODY_ALT = "Tahoma, Segoe UI, sans-serif"  # the same face, labelled the other way round
ODD = "Arial, Helvetica, sans-serif"


def _blank_scan() -> bytes:
    """An image-only page: no text layer, like a scan."""
    doc = pymupdf.open()
    doc.new_page().draw_rect(pymupdf.Rect(50, 50, 200, 100), fill=(0.9, 0.9, 0.9))
    data = doc.tobytes()
    doc.close()
    return data


def _ocr_page(odd: dict[tuple[int, int], dict] | None = None, *, rows: int = 12) -> OCRPage:
    """OCR words on a 12-row, 4-column grid (A4 fractions), all in the body
    face except `odd` cells, whose OCRWord fields are overridden."""
    odd = odd or {}
    words = []
    for r in range(rows):
        for c, text in enumerate(["FEE:", "AMOUNT", f"{5 + r},500", "DUE"]):
            family = BODY if (r + c) % 2 else BODY_ALT
            word = OCRWord(text, 0.1 + c * 0.2, 0.2 + r * 0.04, 0.08, 0.014, family, 0.99)
            for key, value in odd.get((r, c), {}).items():
                setattr(word, key, value)
            words.append(word)
    return OCRPage(page_number=1, words=words)


def _amounts_in(family: str, n: int, **extra) -> dict[tuple[int, int], dict]:
    return {(r, 2): {"font_family": family, **extra} for r in range(n)}


@pytest.mark.parametrize(
    "similar, expected",
    [
        ("Segoe UI, Tahoma, sans-serif", "humanist sans"),
        ("Tahoma, sans-serif", "humanist sans"),
        ("Arial, Helvetica, sans-serif", "grotesque sans"),
        ("Times New Roman, serif", "serif"),
        ("Courier New, monospace", "monospace"),
        ("sans-serif", None),
        ("Some Unknown Face, serif", None),
    ],
)
def test_ocr_font_class(similar, expected):
    assert ocr_font_class(similar) == expected


def test_scan_with_consistent_fonts_is_clean_even_when_labels_alternate():
    result = analyze_font_consistency(_blank_scan(), [_ocr_page()])
    assert result["result"] == "pass"
    assert result["details"][0]["data"]["pages"] == [{"page": 1, "source": "ocr"}]
    assert "1 scanned" in result["details"][0]["description"]


def test_scan_with_amounts_in_another_font_class_is_flagged_medium():
    hits = _font_hits(_blank_scan(), [_ocr_page(_amounts_in(ODD, 6))])
    assert [h["data"]["text"] for h in hits] == [f"{5 + r},500" for r in range(6)]
    assert {h["severity"] for h in hits} == {"medium"}
    assert {h["data"]["source"] for h in hits} == {"ocr"}
    assert hits[0]["data"]["family"] == "grotesque sans" and hits[0]["data"]["expected_family"] == "humanist sans"
    assert "scanned" in hits[0]["description"] and hits[0]["bounding_box"]["page"] == 1


def test_a_single_odd_word_on_a_scan_is_shown_but_not_scored():
    result = analyze_font_consistency(_blank_scan(), [_ocr_page(_amounts_in(ODD, 1))])
    (hit,) = [d for d in result["details"] if d["finding"] == "font_inconsistency"]
    assert hit["data"]["text"] == "5,500" and hit["bounding_box"]["page"] == 1
    assert hit["severity"] == "low" and hit["data"]["words_in_this_font"] == 1
    assert "shown for review but not scored" in hit["description"]
    assert result["result"] == "pass"  # nothing scored


def test_scan_findings_are_scored_once_several_words_corroborate():
    # Four stray labels is within OCR noise on genuine scans; five is not.
    assert {h["severity"] for h in _font_hits(_blank_scan(), [_ocr_page(_amounts_in(ODD, 4))])} == {"low"}
    five = _font_hits(_blank_scan(), [_ocr_page(_amounts_in(ODD, 5))])
    assert len(five) == 5 and {h["severity"] for h in five} == {"medium"}


def test_a_single_odd_run_in_a_digital_pdf_is_always_scored():
    (hit,) = _font_hits(_invoice({(2, 3): (TIMES, None)}))
    assert hit["severity"] == "high" and "not scored" not in hit["description"]


@pytest.mark.parametrize(
    "extra",
    [{"font_confidence": 0.8}, {"italic_or_handwritten": True}, {"height": 0.007}, {"height": 0.03}],
    ids=["low-confidence", "italic-or-handwritten", "small-print", "large-text"],
)
def test_scan_ignores_unreliable_words(extra):
    assert _font_hits(_blank_scan(), [_ocr_page(_amounts_in(ODD, 6, **extra))]) == []


def test_text_layer_wins_over_ocr_on_pages_that_have_one():
    # A digital page is read from its own fonts; OCR estimates for it are ignored.
    assert _font_hits(_invoice(), [_ocr_page(_amounts_in(ODD, 6))]) == []


# --- numeric re-check (B3) ---------------------------------------------------------


def test_numbers_rechecked_once_several_are_flagged():
    # 6 amounts scored in Arial; a 7th lists the body face first ("Segoe UI,
    # Arial") and an 8th has a 0.85 estimate — both the same retyped amounts.
    odd = _amounts_in(ODD, 6)
    odd[(6, 2)] = {"font_family": "Segoe UI, Arial, Helvetica, sans-serif"}
    odd[(7, 2)] = {"font_family": ODD, "font_confidence": 0.85}
    # 16 rows: dropping a word from the analysis must not tip the amount
    # column into "set in that font by design" (most of a column).
    hits = _font_hits(_blank_scan(), [_ocr_page(odd, rows=16)])
    assert [h["data"]["text"] for h in hits] == [f"{5 + r},500" for r in range(8)]
    assert {h["severity"] for h in hits} == {"medium"}
    rechecked = [h for h in hits if h["data"].get("numeric_recheck")]
    assert [h["data"]["text"] for h in rechecked] == ["11,500", "12,500"]
    assert "second look" in rechecked[0]["description"]
    assert {h["data"]["words_in_this_font"] for h in hits} == {8}


def test_recheck_never_applies_to_words_or_below_the_lowered_confidence():
    odd = _amounts_in(ODD, 6)
    odd[(6, 1)] = {"font_family": "Segoe UI, Arial, sans-serif"}  # a word: never re-checked
    odd[(7, 2)] = {"font_family": ODD, "font_confidence": 0.75}   # below 0.95 - 0.15
    hits = _font_hits(_blank_scan(), [_ocr_page(odd, rows=16)])
    assert [h["data"]["text"] for h in hits] == [f"{5 + r},500" for r in range(6)]


def test_no_recheck_without_enough_flagged_numbers():
    # Fewer than 5 odd amounts are not scored, so nothing triggers a re-check.
    odd = _amounts_in(ODD, 4)
    odd[(6, 2)] = {"font_family": "Segoe UI, Arial, sans-serif"}
    hits = _font_hits(_blank_scan(), [_ocr_page(odd)])
    assert len(hits) == 4 and not any(h["data"].get("numeric_recheck") for h in hits)


def test_clean_scan_has_no_recheck_noise():
    page = _ocr_page({(r, 2): {"font_family": "Segoe UI, Arial, sans-serif"} for r in range(12)})
    assert analyze_font_consistency(_blank_scan(), [page])["result"] == "pass"
