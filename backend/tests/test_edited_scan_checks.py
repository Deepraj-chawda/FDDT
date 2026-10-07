"""
Checks for a scan converted to editable text and retyped (CASE-F6F5FE76):
converter subset fonts in font consistency, size changes inside a number,
page structure (vector text over a scan; "limited" pixel checks), PDF-editor
and edited-scan metadata, the printed date vs the file's creation, and
stacked "Description | Amount" tables. Pure logic over in-memory PDFs; no
DB, no external files.
"""
from __future__ import annotations

import io

import pikepdf
import pymupdf
import pytest

from app.services.field_validation_service import validate_fields
from app.services.forensics.font_consistency import analyze_font_consistency, font_family, is_ocr_subset_font
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.forensics.page_structure import analyze_page_structure, limit_pixel_check
from app.services.line_item_parsing import enrich_extracted_fields, parse_stacked_amount_tables, pdf_info
from app.services.ocr_service import OCRTable

# --- builders ------------------------------------------------------------------


def _converted_scan(
    lines: list[list[tuple[str, str, float]]], *, background: bool = True, render_mode: int = 0,
    subset_digits: str | None = "13579", image_on_top: bool = False,
) -> bytes:
    """One page: an optional page-sized background image, and rows of text
    runs (text, font, size). Fonts named "sub" are renamed to the
    converter-style subset "Helvetica-6003", "sub2" to "Times-Roman-6002";
    "full" stays the full Helvetica-Bold. `subset_digits` become the
    Helvetica-6003 ToUnicode (the glyphs that subset holds)."""
    fonts = {"sub": "helv", "sub2": "tiro", "full": "hebo"}
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), False)
    pix.clear_with(235)
    if background and not image_on_top:
        page.insert_image(page.rect, pixmap=pix)
    for row, runs in enumerate(lines):
        x, y = 60.0, 60.0 + row * 38
        for text, font, size in runs:
            page.insert_text((x, y), text, fontname=fonts[font], fontsize=size, render_mode=render_mode)
            x += pymupdf.get_text_length(text, fontname=fonts[font], fontsize=size)
    if background and image_on_top:  # a flattened page pasted over the text
        page.insert_image(page.rect, pixmap=pix, overlay=True)
    data = doc.tobytes()
    doc.close()

    pdf = pikepdf.open(io.BytesIO(data))
    for font in pdf.pages[0].Resources.Font.values():
        base = str(font.BaseFont)
        if base == "/Helvetica":
            font.BaseFont = pikepdf.Name("/Helvetica-6003")
            if subset_digits is not None:
                chars = sorted(set(subset_digits + ",AEDTotalmun "))
                body = "".join(f"<{ord(c):02X}> <{ord(c):04X}>\n" for c in chars)
                cmap = (
                    "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                    f"{len(chars)} beginbfchar\n{body}endbfchar\nendcmap end end\n"
                )
                font.ToUnicode = pikepdf.Stream(pdf, cmap.encode())
        elif base == "/Times-Roman":
            font.BaseFont = pikepdf.Name("/Times-Roman-6002")
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


BODY = [
    [("British Orchard Nursery, Al Bateen, Abu Dhabi", "sub", 10)],
    [("Student Name: Rakan Amara   Registration No: ALB 473", "sub", 10)],
    [("Fees description and amounts for the academic year", "sub", 10)],
] + [[(f"Line {n} of the fee schedule and its terms", "sub", 10)] for n in range(1, 13)]


def _font_hits(pdf: bytes) -> list[dict]:
    return [f for f in analyze_font_consistency(pdf)["details"] if f["severity"] in ("medium", "high")]


# --- font consistency on a converted scan ------------------------------------------


@pytest.mark.parametrize("name", ["Comic Sans MS-Bold-6003", "*Arial-Bold-6000", "ABCDEF+Times New Roman-5999"])
def test_converter_subset_names(name):
    assert is_ocr_subset_font(name)


@pytest.mark.parametrize("name", ["Comic Sans MS-Bold", "ArialMT", "Code-128", "Frutiger-55Roman"])
def test_full_font_names(name):
    assert not is_ocr_subset_font(name)


def test_subset_number_is_not_part_of_the_family():
    assert font_family("*Comic Sans MS-Bold-6003") == font_family("Comic Sans MS-Bold") == "comicsansms"
    assert font_family("*Arial-Bold-6000") == "helvetica"


def test_full_font_digits_among_converter_subsets_are_flagged_with_original_digits():
    pdf = _converted_scan(
        BODY + [[("Total Amount AED 3", "sub", 12), ("6", "full", 11.7), (",", "sub", 12), ("000", "full", 11.7)]]
    )
    hits = _font_hits(pdf)
    assert sorted(h["data"]["text"] for h in hits) == ["000", "6"]
    for hit in hits:
        assert hit["finding"] == "font_inconsistency" and hit["severity"] == "high"
        assert hit["data"]["converted_scan"] is True
        assert hit["data"]["expected_font"] == "Helvetica-6003"
        assert hit["data"]["original_subset_digits"] == ["1", "3", "5", "7", "9"]
        assert hit["data"]["size_change"]["sizes"] == [11.7, 12.0]
        assert "typed in after the conversion" in hit["description"]
    six = next(h for h in hits if h["data"]["text"] == "6")
    assert six["data"]["digits_not_in_original"] == ["6"]


def test_two_converter_subsets_side_by_side_are_not_evidence():
    # "DATE OF ISSUE 14 03 2022": the converter guessed another style for "03".
    pdf = _converted_scan(BODY[:3] + [[("DATE OF ISSUE 14 ", "sub", 7), ("03", "sub2", 7), (" 2022", "sub", 7)]])
    assert _font_hits(pdf) == []
    pages = analyze_font_consistency(pdf)["details"][0]["data"]["pages"]
    assert pages == [{"page": 1, "source": "text_layer", "converted_scan": True}]


def test_glyph_hint_is_left_out_without_a_to_unicode_map():
    pdf = _converted_scan(BODY + [[("Total Amount AED 3", "sub", 12), ("6", "full", 12)]], subset_digits=None)
    (hit,) = _font_hits(pdf)
    assert "original_subset_digits" not in hit["data"]


# --- size change inside one number (any text-layer page) -------------------------------


def _digital(runs: list[tuple[str, float]]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Invoice 2291 for consulting services rendered in August", fontname="helv", fontsize=9)
    x = 50.0
    for text, size in runs:
        page.insert_text((x, 100), text, fontname="helv", fontsize=size)
        x += pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    data = doc.tobytes()
    doc.close()
    return data


def test_number_with_mixed_character_sizes_is_flagged():
    hits = _font_hits(_digital([("Total: 1", 9), ("8", 8.7), (",450.00", 9)]))
    assert [(h["finding"], h["data"]["text"], h["data"]["sizes"]) for h in hits] == [
        ("font_size_inconsistency", "18,450.00", [8.7, 9.0])
    ]


@pytest.mark.parametrize(
    "runs",
    [
        [("Total: 1", 9), ("8", 8.9), (",450.00", 9)],  # below 0.2 pt
        [("1", 9), ("st", 6), (" TERM 12,000", 9)],  # a superscript ordinal is not a number
        [("Total: 18,450.00", 9)],
    ],
)
def test_no_size_finding_for_uniform_numbers_or_ordinals(runs):
    assert _font_hits(_digital(runs)) == []


# --- page structure and pixel checks ------------------------------------------------------


def test_converted_scan_structure():
    (page,) = analyze_page_structure(_converted_scan(BODY))
    assert page.background_image and page.vector_text_over_image and page.editable_text_over_scan


def test_searchable_scan_with_invisible_ocr_text_is_normal():
    (page,) = analyze_page_structure(_converted_scan(BODY, render_mode=3))
    assert page.background_image and not page.vector_text_over_image and not page.editable_text_over_scan


def test_digital_pdf_without_a_background_image_is_normal():
    (page,) = analyze_page_structure(_converted_scan(BODY, background=False))
    assert not page.vector_text_over_image


def test_an_image_pasted_over_the_text_is_not_a_background():
    (page,) = analyze_page_structure(_converted_scan(BODY, image_on_top=True))
    assert page.visible_runs_over_image == 0 and not page.vector_text_over_image


def test_a_few_labels_over_a_scan_are_not_the_page_text():
    (page,) = analyze_page_structure(_converted_scan(BODY[:3]))
    assert page.background_image and not page.vector_text_over_image


def test_pixel_check_pass_becomes_limited_on_vector_text_over_a_scan():
    structure = analyze_page_structure(_converted_scan(BODY))
    limited = limit_pixel_check({"result": "pass", "details": []}, structure, "error level analysis")
    assert limited["result"] == "limited"
    assert limited["details"][0]["finding"] == "pixel_analysis_limited"
    assert limited["details"][0]["severity"] == "info"
    assert "cannot see an edit to that text" in limited["details"][0]["description"]
    flagged = {"result": "flag", "details": [{"finding": "copy_move_cluster", "severity": "high"}]}
    assert limit_pixel_check(flagged, structure, "copy-move detection") is flagged
    normal = analyze_page_structure(_converted_scan(BODY, render_mode=3))
    assert limit_pixel_check({"result": "pass", "details": []}, normal, "ELA")["result"] == "pass"


# --- metadata ----------------------------------------------------------------------------


def _with_metadata(pdf_bytes: bytes, info: dict[str, str], history: list[tuple[str, str, str | None]] = ()) -> bytes:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    for key, value in info.items():
        pdf.docinfo[key] = value
    if history:
        entries = "".join(
            '<rdf:li rdf:parseType="Resource">'
            f"<stEvt:action>{action}</stEvt:action><stEvt:when>{when}</stEvt:when>"
            + (f"<stEvt:softwareAgent>{agent}</stEvt:softwareAgent>" if agent else "")
            + "</rdf:li>"
            for action, when, agent in history
        )
        xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" '
            'xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#">'
            f"<xmpMM:History><rdf:Seq>{entries}</rdf:Seq></xmpMM:History>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
        )
        stream = pikepdf.Stream(pdf, xmp.encode())
        stream.Type, stream.Subtype = pikepdf.Name("/Metadata"), pikepdf.Name("/XML")
        pdf.Root.Metadata = stream
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _metadata_findings(pdf: bytes) -> dict[str, dict]:
    return {f["finding"]: f for f in analyze_pdf_metadata(pdf)["details"]}


@pytest.mark.parametrize("producer", ["iLovePDF", "Smallpdf.com", "Sejda Online", "PDF-XChange Editor v9.4"])
def test_pdf_editor_producer_is_reported(producer):
    found = _metadata_findings(_with_metadata(_converted_scan(BODY), {"/Producer": producer}))
    assert found["pdf_editor_detected"]["severity"] == "medium"
    assert "editing_software_detected" not in found


@pytest.mark.parametrize("producer", ["Microsoft: Print To PDF", "PDF-XChange Standard printer", "Canon iR-ADV C5535"])
def test_print_drivers_and_scanners_are_not_pdf_editors(producer):
    assert "pdf_editor_detected" not in _metadata_findings(_with_metadata(_converted_scan(BODY), {"/Producer": producer}))


def test_edited_scanned_document_history_is_flagged_high():
    pdf = _with_metadata(_converted_scan(BODY), {}, [("editedScannedDoc", "2023-10-24T14:27:53+04:00", None)])
    found = _metadata_findings(pdf)
    assert found["history_scanned_document_edited"]["severity"] == "high"
    assert found["history_scanned_document_edited"]["data"]["action"] == "editedScannedDoc"


def test_history_saved_by_a_pdf_editor_is_a_tool_anomaly():
    pdf = _with_metadata(_converted_scan(BODY), {}, [("saved", "2024-09-04T11:42:25+04:00", "iLovePDF")])
    found = _metadata_findings(pdf)
    assert found["history_editing_tool_anomaly"]["data"]["software_agent"] == "iLovePDF"
    assert "history_scanned_document_edited" not in found


def test_editable_text_over_scan_is_scored_with_converter_fonts_even_without_xmp():
    found = _metadata_findings(_converted_scan(BODY))  # no XMP at all
    assert "xmp_history" in found and found["xmp_history"]["data"]["entries"] == []
    assert found["editable_text_over_scan"]["severity"] == "medium"
    assert found["editable_text_over_scan"]["data"]["converter_font_pages"] == [1]
    assert "editable_text_over_scan" not in _metadata_findings(_converted_scan(BODY, render_mode=3))


def test_vector_text_over_a_scan_is_scored_whatever_the_fonts_are_called():
    # Acrobat's own editor (or a re-saved converted file) leaves ordinary subset fonts, not -NNNN ones.
    plain = [[(text, "full", size) for text, _, size in row] for row in BODY]
    found = _metadata_findings(_converted_scan(plain))
    assert found["editable_text_over_scan"]["severity"] == "medium"
    assert found["editable_text_over_scan"]["data"]["converter_font_pages"] == []


# --- printed date vs the file's creation --------------------------------------------------


def _date_check(printed: str | None, info: dict | None) -> dict:
    core = {"date": {"value": printed}} if printed else {}
    fields = {"core_fields": core, "additional_fields": [], "line_items": []}
    if info is not None:
        fields["pdf_info"] = info
    return validate_fields(fields)["details"]["document_date_vs_file_creation"]


def test_document_dated_after_its_file_was_created_and_edited_since_is_flagged():
    sub = _date_check("2024-09-02", {"CreationDate": "2023-10-24T14:23:28+04:00", "ModDate": "2024-09-04T11:42:25+04:00"})
    assert sub["status"] == "flag"
    assert "created on 2023-10-24 — 314 days earlier — and last modified on 2024-09-04" in sub["reason"]


@pytest.mark.parametrize(
    "printed, info, status",
    [
        # generated in advance, never modified after: a term invoice
        ("2024-09-02", {"CreationDate": "2024-08-20T10:00:00+04:00", "ModDate": "2024-08-20T10:00:00+04:00"}, "pass"),
        ("2024-09-02", {"CreationDate": "2024-08-20T10:00:00+04:00"}, "pass"),
        # within a day
        ("2024-09-02", {"CreationDate": "2024-09-01T23:00:00+04:00", "ModDate": "2024-09-03T10:00:00+04:00"}, "pass"),
        ("2024-09-02", {"CreationDate": "2024-09-05T10:00:00+04:00"}, "pass"),
        # an unset scanner clock
        ("2024-09-02", {"CreationDate": "2000-01-01T00:00:00+00:00", "ModDate": "2024-09-03T00:00:00+00:00"}, "skipped"),
        ("2024-09-02", {"Producer": "iLovePDF"}, "skipped"),
        ("2024-09-02", None, "skipped"),
        (None, {"CreationDate": "2023-10-24T14:23:28+04:00"}, "skipped"),
    ],
)
def test_document_date_vs_file_creation_does_not_flag(printed, info, status):
    assert _date_check(printed, info)["status"] == status


def test_pdf_info_records_the_file_dates():
    pdf = _with_metadata(_converted_scan(BODY), {"/CreationDate": "D:20231024142328+04'00'", "/ModDate": "D:20240904114225+04'00'"})
    info = pdf_info(pdf)
    assert info["CreationDate"] == "2023-10-24T14:23:28+04:00"
    assert info["ModDate"] == "2024-09-04T11:42:25+04:00"


# --- stacked "Description | Amount" tables -----------------------------------------------


def _table(rows: list[list[str]]) -> OCRTable:
    cells = [
        {"row_index": r, "column_index": c, "content": text}
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
        if text
    ]
    return OCRTable(row_count=len(rows), column_count=max(len(r) for r in rows), cells=cells)


BRITISH_ORCHARD = [
    ["Student Name:Rakan Amara", "Parents Name: Mohamed Faouz Amara"],
    ["Fees description", "Amount"],
    ["15 TERM", "12,000 9,000 9,000 6,000"],
    ["2ND TERM", ""],
    ["3rd TERM", ""],
    ["SUMMER CAMPS", ""],
    ["Total Amount", "AED 36,000"],
]


def test_amounts_stacked_in_one_cell_are_paired_with_description_rows():
    parsed = parse_stacked_amount_tables([_table(BRITISH_ORCHARD)])
    assert [(i["description"], i["line_total"]) for i in parsed["line_items"]] == [
        ("15 TERM", 12000), ("2ND TERM", 9000), ("3rd TERM", 9000), ("SUMMER CAMPS", 6000),
    ]
    assert parsed["total"] == (36000, "AED 36,000")


def test_descriptions_stacked_in_one_cell_one_per_line():
    rows = [["Item description", "Amount"], ["Tuition\nBooks\nUniform", "9,000\n850\n400"], ["Total", "10,250"]]
    parsed = parse_stacked_amount_tables([_table(rows)])
    assert [(i["description"], i["line_total"]) for i in parsed["line_items"]] == [
        ("Tuition", 9000), ("Books", 850), ("Uniform", 400),
    ]


@pytest.mark.parametrize(
    "rows",
    [
        # one amount per row: an ordinary table, left to the extraction
        [["Fees description", "Amount"], ["Term 1", "12,000"], ["Term 2", "9,000"], ["Total", "21,000"]],
        # counts disagree: no pairing is safe
        [["Fees description", "Amount"], ["Term 1", "12,000 9,000 6,000"], ["Term 2", ""], ["Total", "27,000"]],
        # the amount column holds text
        [["Fees description", "Amount"], ["Term 1", "12,000 see note"], ["Term 2", ""]],
        # no description/amount header
        [["Name", "Grade"], ["A", "12,000 9,000"], ["B", ""]],
    ],
)
def test_other_tables_are_not_read_as_stacked(rows):
    assert parse_stacked_amount_tables([_table(rows)]) is None


def test_enrich_replaces_llm_lines_and_fills_the_total():
    llm = [
        {"description": "1st TERM", "line_total": 12000.0, "bounding_box": {"page": 1}},
        {"description": "2ND TERM", "line_total": 9000.0},
    ]
    fields = enrich_extracted_fields(
        {"line_items": llm, "core_fields": {"amount": {"value": None}}}, ocr_tables=[_table(BRITISH_ORCHARD)]
    )
    assert [i["source"] for i in fields["line_items"]] == ["table"] * 4
    assert fields["line_items"][0]["bounding_box"] == {"page": 1}
    assert fields["core_fields"]["amount"]["value"] == 36000


# --- metadata score cap ------------------------------------------------------------------


def _fired(*rules: tuple[str, float]):
    from types import SimpleNamespace

    return [SimpleNamespace(rule=SimpleNamespace(rule_id=rule_id, weight=weight)) for rule_id, weight in rules]


def test_metadata_rules_together_count_at_most_the_cap():
    from app.services.risk_scoring_service import capped_raw_score

    fired = _fired(
        ("font.inconsistency", 40), ("metadata.modified_after_creation", 15),
        ("metadata.document_date_after_file_creation", 25), ("metadata.history_scanned_doc_edited", 15),
        ("metadata.pdf_editor_producer", 10), ("metadata.editable_text_over_scan", 10),
    )
    assert capped_raw_score(fired, 40) == (80.0, {"metadata": {"cap": 40, "points": 75}})
    assert capped_raw_score(fired, 100) == (115.0, {})
    # Under the cap: nothing changes, nothing recorded.
    assert capped_raw_score(_fired(("metadata.rescan_conflict", 10), ("field.date_in_future", 25)), 40) == (35.0, {})


# --- form template date ------------------------------------------------------------------

FOOTER = (
    "Total Term fees are payable by card or cheque.\n"
    "Any Hard copy of this document will be considered as 'UNCONTROLLED COPY'.\nPage 1 of 1\n"
    "DOCUMENT F-BON- No- 180\nREV. No. -\n04\nISSUE No .- 71\nDATE OF ISSUE -\n14.03.2022\n"
)


def test_document_control_date_of_issue_is_labelled_form_template_date():
    from app.services.extraction_postprocess import label_form_template_dates

    fields = {"additional_fields": [{"field_name": "date_of_issue", "value": "14.03.2022", "bounding_box": {"page": 1}}]}
    label_form_template_dates(fields, FOOTER)
    (field,) = fields["additional_fields"]
    assert field["field_name"] == "form_template_date" and field["field_name_as_extracted"] == "date_of_issue"
    assert (field["value"], field["raw_text"], field["bounding_box"]) == ("2022-03-14", "14.03.2022", {"page": 1})
    # Not extracted by the model: added.
    fields = {"additional_fields": []}
    label_form_template_dates(fields, FOOTER)
    assert fields["additional_fields"][0]["field_name"] == "form_template_date"


def test_a_plain_date_of_issue_is_the_documents_own_date():
    from app.services.extraction_postprocess import label_form_template_dates

    fields = {"additional_fields": [{"field_name": "date_of_issue", "value": "2024-09-02"}]}
    label_form_template_dates(fields, "CERTIFICATE OF ENROLMENT\nDate of issue: 02/09/2024\nPrincipal")
    assert fields["additional_fields"][0]["field_name"] == "date_of_issue"


# --- signature ink read as a name ---------------------------------------------------------


def _ocr_page(words):
    from app.services.ocr_service import OCRPage, OCRWord

    return OCRPage(
        page_number=1,
        words=[
            OCRWord(text=t, x=x, y=y, width=w, height=h, font_family=None, font_confidence=None, italic_or_handwritten=hw)
            for t, x, y, w, h, hw in words
        ],
    )


PAGE = [
    ("Prepared", 0.162, 0.558, 0.07, 0.015, False), ("by:", 0.239, 0.558, 0.018, 0.015, False),
    ("Princess", 0.261, 0.558, 0.066, 0.015, False), ("Dalab", 0.184, 0.574, 0.105, 0.045, True),
    ("Ms.Rahab", 0.163, 0.621, 0.075, 0.013, False), ("Awad", 0.246, 0.621, 0.04, 0.014, False),
    ("Thanks", 0.4, 0.3, 0.05, 0.016, True),  # italic body text, normal size
] + [(f"w{n}", 0.1, 0.1 + n * 0.02, 0.05, 0.015, False) for n in range(10)]


def test_signature_read_as_a_word_is_dropped_from_the_name():
    from app.services.extraction_postprocess import strip_signature_text

    box = {"page": 1, "x": 0.1845, "y": 0.5583, "width": 0.1424, "height": 0.0609}
    fields = {"additional_fields": [
        {"field_name": "prepared_by", "value": "Princess Dalab", "bounding_box": box},
        {"field_name": "student_name", "value": "Dalab Amara"},  # not a signer's field
        {"field_name": "principal", "value": "Ms.Rahab Awad"},
    ]}
    strip_signature_text(fields, [_ocr_page(PAGE)])
    prepared, student, principal = fields["additional_fields"]
    assert (prepared["value"], prepared["value_as_read"]) == ("Princess", "Princess Dalab")
    assert "'Dalab'" in prepared["note"]
    assert student["value"] == "Dalab Amara" and principal["value"] == "Ms.Rahab Awad"


def test_detected_signature_region_also_marks_ink():
    from app.services.extraction_postprocess import strip_signature_text

    page = [(t, x, y, w, h, False) for t, x, y, w, h, _ in PAGE]  # OCR missed the handwriting
    region = {"kind": "signature", "bounding_box": {"page": 1, "x": 0.18, "y": 0.57, "width": 0.12, "height": 0.05}}
    fields = {"additional_fields": [{"field_name": "signed_by", "value": "Princess Dalab"}]}
    strip_signature_text(fields, [_ocr_page(page)], [region])
    assert fields["additional_fields"][0]["value"] == "Princess"


# --- stamp vs issuer ------------------------------------------------------------------------

ISSUER = {"issuer": {"value": "British Orchard Nursery Al Bateen Abu Dhabi"}}


def _stamp(text):
    details = validate_fields({"core_fields": ISSUER}, stamps=[{"kind": "stamp", "text": text}] if text else [])
    return details["details"]["stamp_issuer_consistency"]


@pytest.mark.parametrize(
    "text", ["BRITISH ORCHARD NURSERY Br.1 Abu Dhabi Tel. 02 8217901", "British Orchard Nursery P.O. Box 38550"]
)
def test_stamp_naming_the_issuer_passes(text):
    assert _stamp(text)["status"] == "pass"


@pytest.mark.parametrize("text", ["Little Stars Nursery Abu Dhabi", "Emirates Trading LLC Dubai"])
def test_stamp_naming_another_organisation_is_flagged(text):
    result = _stamp(text)
    assert result["status"] == "flag" and f"The stamp reads '{text}'" in result["reason"]


def test_stamp_check_skips_without_text_detection_or_a_shared_script():
    assert _stamp(None)["status"] == "skipped"
    assert validate_fields({"core_fields": ISSUER})["details"]["stamp_issuer_consistency"]["status"] == "skipped"
    assert _stamp("حضانة بريتش أورشارد")["status"] == "skipped"


def test_enriching_twice_does_not_duplicate_table_lines():
    once = enrich_extracted_fields({"line_items": []}, ocr_tables=[_table(BRITISH_ORCHARD)])
    twice = enrich_extracted_fields(once, ocr_tables=[_table(BRITISH_ORCHARD)])
    assert [i["line_total"] for i in twice["line_items"]] == [12000, 9000, 9000, 6000]
