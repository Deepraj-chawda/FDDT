"""
Pairing each amount written in words with the figure printed for it
(app/services/amount_words_layout.py, app/services/amount_words.py's
find_amount_words) and checking every pair against its own figure
(field_validation_service's amount_in_words_consistency). Modelled on the
Build Right Construction quotation, which spells out its total, VAT and grand
total on three rows — and gets all three wrong. Pure logic, no DB.
"""
from app.services.amount_words import amount_kind, find_amount_words, parse_figure
from app.services.amount_words_layout import merge_amounts_in_words, parse_amounts_in_words
from app.services.check_summaries import summarize_check
from app.services.field_locator_service import attach_field_locations
from app.services.field_validation_service import validate_fields
from app.services.line_item_parsing import enrich_extracted_fields
from app.services.ocr_service import OCRPage, OCRTable, OCRWord

H = 0.012


def _line(text: str, x: float, y: float, width: float = 0.2) -> OCRWord:
    return OCRWord(text, x, y, width, H)


def _amount(value: float | None) -> dict:
    return {"value": value, "currency": "AED", "raw_text": None, "confidence": 0.9, "uncertain": False}


# The quotation's totals block, as Azure Layout reads it: three rows, each a
# label line, a words line below it, and the figure (with its own label) at
# the right of the row.
QUOTATION_LINES = [
    _line("1", 0.09, 0.30, 0.01), _line("AUTOMATIC SLIDING ENTRANCE", 0.14, 0.30, 0.25),
    _line("14,800.00", 0.47, 0.30, 0.07), _line("2", 0.58, 0.30, 0.01), _line("No.", 0.62, 0.30, 0.03),
    _line("29,800", 0.70, 0.30, 0.06), _line("1,490", 0.83, 0.30, 0.05),
    _line("Total Amount in Words (AED):", 0.09, 0.42, 0.25),
    _line("Twenty-Nine Thousand Six Hundred Dirhams", 0.09, 0.44, 0.30),
    _line("Total in AED", 0.69, 0.42, 0.08), _line("29,800", 0.80, 0.42, 0.06),
    _line("VAT Amount in Words (AED):", 0.09, 0.47, 0.25),
    _line("One Thousand Four Hundred Eighty Dirhams", 0.09, 0.49, 0.30),
    _line("VAT Total in", 0.69, 0.47, 0.08), _line("AED", 0.71, 0.485, 0.03), _line("1,490", 0.80, 0.47, 0.05),
    _line("Grand Total Amount in Words:", 0.09, 0.52, 0.25),
    _line("Thirty-One Thousand Eighty Dirhams", 0.09, 0.555, 0.30),
    _line("Grand Total", 0.69, 0.52, 0.08), _line("in", 0.72, 0.535, 0.02), _line("AED", 0.71, 0.55, 0.03),
    _line("31,290", 0.80, 0.52, 0.06),
    _line("Payment Terms:", 0.09, 0.60, 0.1),
]


def _page() -> OCRPage:
    page = OCRPage(page_number=1, lines=list(QUOTATION_LINES))
    page.words = [OCRWord(w, l.x + i * 0.05, l.y, 0.04, H) for l in QUOTATION_LINES for i, w in enumerate(l.text.split())]
    return page


def _table() -> OCRTable:
    def cell(row, col, content):
        return {"row_index": row, "column_index": col, "content": content}

    return OCRTable(row_count=5, column_count=7, cells=[
        cell(0, 0, "SN"), cell(0, 1, "Description"), cell(0, 2, "Unit Price"), cell(0, 3, "Qty"),
        cell(0, 4, "Unit"), cell(0, 5, "Total Amount"), cell(0, 6, "VAT 5%"),
        cell(1, 0, "1"), cell(1, 1, "AUTOMATIC SLIDING ENTRANCE DOOR SYSTEM\nClear opening: 1,800 mm (W) x 2,400 mm (H)"),
        cell(1, 2, "14,800.00"), cell(1, 3, "2"), cell(1, 4, "No."), cell(1, 5, "29,800"), cell(1, 6, "1,490"),
        cell(2, 0, "Total Amount in Words (AED):\nTwenty-Nine Thousand Six Hundred Dirhams"),
        cell(2, 5, "Total in AED"), cell(2, 6, "29,800"),
        cell(3, 0, "VAT Amount in Words (AED):\nOne Thousand Four Hundred Eighty Dirhams"),
        cell(3, 5, "VAT Total in AED"), cell(3, 6, "1,490"),
        cell(4, 0, "Grand Total Amount in Words:\nThirty-One Thousand Eighty Dirhams"),
        cell(4, 5, "Grand Total in AED"), cell(4, 6, "31,290"),
    ])


# --- reading one line / cell -----------------------------------------------------


def test_find_amount_words_reads_label_words_and_inline_figure():
    found = find_amount_words("Total Amount in Words (AED):\nTwenty-Nine Thousand Six Hundred Dirhams")
    assert (found["label"], found["value"], found["figure"]) == ("Total Amount in Words (AED)", 29600, None)
    found = find_amount_words("Total: AED 20,500 (Twenty Thousand Five Hundred Only)")
    assert (found["label"], found["value"], found["figure"], found["figure_raw_text"]) == ("Total", 20500, 20500, "AED 20,500")
    assert find_amount_words("Clear opening: 1,800 mm (W) x 2,400 mm (H)") is None
    assert find_amount_words("Total in AED") is None
    assert find_amount_words("One") is None  # a stray word, not an amount
    assert find_amount_words("Say Twenty Thousand Only")["value"] == 20000


def test_parse_figure_and_amount_kind():
    assert parse_figure("29,800") == 29800 and parse_figure("AED 1,490.50") == 1490.5 and parse_figure("31,290/-") == 31290
    assert parse_figure("2 x 14,800") is None and parse_figure("No.") is None
    assert amount_kind("Total Amount in Words (AED)") == "total"
    assert amount_kind("VAT Amount in Words (AED)") == "tax"
    assert amount_kind("Grand Total Amount in Words") == "grand_total"
    assert amount_kind("VAT Total in AED") == "tax" and amount_kind("Say") is None


# --- pairing from the layout -------------------------------------------------------


def _by_value(pairs):
    return {p["value"]: p for p in pairs}


def test_table_rows_pair_words_with_the_figure_in_the_same_row():
    pairs = _by_value(parse_amounts_in_words(None, [_table()], None))
    assert set(pairs) == {29600, 1480, 31080}
    assert pairs[29600]["figure"] == 29800 and pairs[29600]["figure_label"] == "Total in AED"
    assert pairs[1480]["figure"] == 1490 and pairs[1480]["label"] == "VAT Amount in Words (AED)"
    assert pairs[31080]["figure"] == 31290 and pairs[31080]["source"] == "table"


def test_page_geometry_pairs_words_with_the_figure_beside_them_and_boxes_both():
    pairs = _by_value(parse_amounts_in_words(None, None, [_page()]))
    assert set(pairs) == {29600, 1480, 31080}
    total = pairs[29600]
    assert total["figure"] == 29800 and total["label"] == "Total Amount in Words (AED)" and total["figure_label"] == "Total in AED"
    assert total["bounding_box"]["y"] == 0.44 and total["figure_bounding_box"]["x"] == 0.80
    # A figure label wrapped over several lines ("VAT Total in" / "AED") keeps the part on the figure's line.
    assert pairs[1480]["figure"] == 1490 and pairs[1480]["figure_label"] == "VAT Total in"
    assert pairs[31080]["figure"] == 31290 and pairs[31080]["figure_label"] == "Grand Total"


def test_wrapped_words_are_joined_and_a_stacked_totals_block_is_not_guessed():
    lines = [
        _line("Amount in words:", 0.09, 0.40, 0.12),
        _line("Fifty-Five Thousand Five Hundred", 0.09, 0.42, 0.25),
        _line("Dirhams Only", 0.09, 0.435, 0.1),
        # Subtotal / VAT / Total stacked at the right, all within the words' row band.
        _line("Subtotal", 0.65, 0.40, 0.06), _line("52,857.14", 0.80, 0.40, 0.07),
        _line("VAT 5%", 0.65, 0.42, 0.06), _line("2,642.86", 0.80, 0.42, 0.07),
        _line("Total", 0.65, 0.44, 0.06), _line("55,500.00", 0.80, 0.44, 0.07),
    ]
    [pair] = parse_amounts_in_words(None, None, [OCRPage(page_number=1, lines=lines)])
    assert pair["text"] == "Fifty-Five Thousand Five Hundred Dirhams Only" and pair["value"] == 55500
    assert pair["label"] == "Amount in words"
    # "Amount in words" names no particular figure, three share the band: left unpaired.
    assert pair["figure"] is None
    # With a label that names the figure, the matching one is taken.
    lines[0] = _line("Total in words:", 0.09, 0.40, 0.12)
    [pair] = parse_amounts_in_words(None, None, [OCRPage(page_number=1, lines=lines)])
    assert pair["figure"] == 55500 and pair["figure_label"] == "Total"


def test_plain_text_pairs_only_what_is_on_the_same_line():
    text = "Total: AED 20,500 (Twenty Thousand Five Hundred Only)\nAmount in words:\nFive Hundred Dirhams Only\nTotal 600"
    pairs = _by_value(parse_amounts_in_words(text, None, None))
    assert pairs[20500]["figure"] == 20500
    assert pairs[500]["figure"] is None and pairs[500]["label"] == "Amount in words"


def test_model_entries_fill_in_what_the_layout_missed_and_never_duplicate():
    layout = parse_amounts_in_words(None, [_table()], None)
    llm = [
        {"label": None, "text": "Twenty-Nine Thousand Six Hundred Dirhams", "value": 29800, "figure": 29800,
         "figure_raw_text": "29,800", "confidence": 0.9},
        {"label": None, "text": "فقط خمسة آلاف درهم", "value": 5000, "figure": None, "figure_raw_text": None, "confidence": 0.8},
    ]
    merged = merge_amounts_in_words(llm, layout)
    assert [p["source"] for p in merged] == ["table", "table", "table", "llm"]
    assert merged[0]["value"] == 29600  # the layout's own reading of the words, not the model's


def test_enrich_replaces_the_single_slot_with_the_paired_list():
    # What the single-slot extraction produced for this quotation: all three
    # phrases run together in one entry.
    run_together = ("Twenty-Nine Thousand Six Hundred Dirhams One Thousand Four Hundred Eighty Dirhams "
                    "Thirty-One Thousand Eighty Dirhams")
    fields = enrich_extracted_fields(
        {"core_fields": {"amount": _amount(31290.0)}, "additional_fields": [], "line_items": [],
         "amount_in_words": {"text": run_together, "value": 1141080, "confidence": 0.9}},
        ocr_tables=[_table()], ocr_pages=[_page()],
    )
    assert "amount_in_words" not in fields
    assert sorted(p["value"] for p in fields["amounts_in_words"]) == [1480, 29600, 31080]
    # The run-together text is kept as the model's entry only if it reads as an amount — it does not pair with any row.
    assert all(p["source"] in ("table", "layout") for p in fields["amounts_in_words"][:3])


# --- the check -------------------------------------------------------------------


def _validate(core: dict, pairs: list) -> dict:
    return validate_fields({"core_fields": core, "additional_fields": [], "line_items": [],
                            "amounts_in_words": pairs})["details"]["amount_in_words_consistency"]


CORE = {"subtotal": _amount(29800.0), "tax_amount": _amount(1490.0), "amount": _amount(31290.0)}


def test_each_row_is_checked_against_its_own_figure():
    pairs = parse_amounts_in_words(None, None, [_page()])
    result = _validate(CORE, pairs)
    assert result["status"] == "flag"
    reason = result["reason"]
    assert "The total in words, 'Twenty-Nine Thousand Six Hundred Dirhams', is 29,600.00 (read from the words), but the total in figures is 29,800.00." in reason
    assert "The VAT in words, 'One Thousand Four Hundred Eighty Dirhams', is 1,480.00 (read from the words), but the VAT in figures is 1,490.00." in reason
    assert "The grand total in words, 'Thirty-One Thousand Eighty Dirhams', is 31,080.00 (read from the words), but the grand total in figures is 31,290.00." in reason
    assert "1,141,080" not in reason
    # Each figure and each words line is highlighted, figures from the layout's own boxes.
    assert [r["field"] for r in result["regions"]] == [
        "amount_in_words_1_figure", "amount_in_words_1", "amount_in_words_2_figure", "amount_in_words_2",
        "amount_in_words_3_figure", "amount_in_words_3",
    ]
    assert result["regions"][0]["value"] == "29,800" and result["regions"][0]["label"] == "Total in AED"

    summary = summarize_check("field_validation", "completed", {"result": "flag", "details": {"amount_in_words_consistency": result}})
    [item] = summary["items"]
    assert item["text"] == (
        "words say 29,600.00, the total in figures is 29,800.00; words say 1,480.00, the VAT in figures is 1,490.00; "
        "words say 31,080.00, the grand total in figures is 31,290.00"
    )


def test_rows_that_agree_pass_together():
    pairs = [
        {"label": "Total Amount in Words", "text": "Twenty-Nine Thousand Eight Hundred Dirhams", "value": 29800, "figure": 29800},
        {"label": "VAT Amount in Words", "text": "One Thousand Four Hundred Ninety Dirhams", "value": 1490, "figure": 1490},
        {"label": "Grand Total Amount in Words", "text": "Thirty-One Thousand Two Hundred Ninety Dirhams", "value": 31290, "figure": 31290.0},
    ]
    result = _validate(CORE, pairs)
    assert result["status"] == "pass" and result["reason"].startswith("All 3 amounts in words match their figures")


def test_words_without_a_paired_figure_are_compared_with_the_field_their_label_names():
    vat = {"label": "VAT Amount in Words", "text": "One Thousand Four Hundred Eighty Dirhams", "value": 1480, "figure": None}
    result = _validate(CORE, [vat])
    assert result["status"] == "flag"
    assert "but the VAT in figures is 1,490.00" in result["reason"] and "31,290" not in result["reason"]
    # Never the grand total: VAT words that match the tax amount pass.
    vat["text"] = "One Thousand Four Hundred Ninety Dirhams"
    assert _validate(CORE, [vat])["status"] == "pass"
    # A document with no tax amount extracted: nothing to compare VAT words with.
    result = _validate({"amount": _amount(31290.0)}, [vat])
    assert result["status"] == "skipped" and "No figure to compare" in result["reason"]


def test_only_the_wrong_row_is_reported():
    pairs = [
        {"label": "Total Amount in Words", "text": "Twenty-Nine Thousand Eight Hundred Dirhams", "value": 29800, "figure": 29800},
        {"label": "Grand Total Amount in Words", "text": "Thirty-One Thousand Eighty Dirhams", "value": 31080, "figure": 31290},
    ]
    result = _validate(CORE, pairs)
    assert result["status"] == "flag" and result["reason"].count("read from the words") == 1
    assert "grand total in figures is 31,290.00" in result["reason"]


def test_locator_boxes_the_paired_figure_next_to_the_words():
    page = _page()
    fields = {"core_fields": {}, "additional_fields": [], "line_items": [],
              "amounts_in_words": [{"label": "VAT Amount in Words (AED)", "text": "One Thousand Four Hundred Eighty Dirhams",
                                    "value": 1480, "figure": 1490, "confidence": 0.9}]}
    attach_field_locations([page], fields)
    [pair] = fields["amounts_in_words"]
    assert pair["bounding_box"]["y"] == 0.49
    # 1,490 is printed twice (the VAT column of the item row, and the VAT total): the one on the words' row.
    assert pair["figure_bounding_box"]["y"] == 0.47
