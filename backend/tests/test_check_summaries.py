"""
Short check summaries (app/services/check_summaries.py) and the vision
model's hint on deleted blocks (ghost_content.add_vision_hints).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.check_summaries import first_sentence, risk_reason_short, summarize_check, summarize_document_checks
from app.services.forensics.ghost_content import _agreed_hint


def _split(text: str, subset: str, **data) -> dict:
    return {
        "finding": "font_subset_split", "severity": "high", "page": 1,
        "description": f"Page 1: '{text}' is set in a second embedded copy of TimesNewRoman (subset {subset}).",
        "data": {"text": text, "subset": subset, "font": "TimesNewRoman", **data},
    }


def test_repeated_font_findings_become_one_line_with_the_values():
    findings = [_split(v, "JIXQHL", digits_only_subset=True, same_column_as="4,575.00")
                for v in ["1,450.00", "16,450.00", "13,350.00", "13,350.00"]]
    findings += [_split("AL REEM", "MONKPZ", same_row_as="SALEH MOHAMED ALHARTHI"),
                 _split("2820", "MONKPZ", same_row_as="SALEH MOHAMED ALHARTHI")]
    summary = summarize_check("font_consistency", "completed", {"result": "flag", "details": findings})
    by_title = {i["title"]: i for i in summary["items"]}
    assert set(by_title) == {"Amounts retyped", "Text retyped"}
    amounts = by_title["Amounts retyped"]
    assert amounts["text"] == (
        "1,450.00 · 16,450.00 · 13,350.00 ×2 — in a second copy of Times New Roman (a digits-only copy); "
        "4,575.00 in the same column is original"
    )
    assert amounts["detail"].endswith("(3 more of the same kind.)")
    assert by_title["Text retyped"]["text"].endswith("'SALEH MOHAMED ALHARTHI' on the same line is original")
    assert summary["headline"] == "2 issues: Amounts retyped, Text retyped"


def test_metadata_lines_carry_the_values():
    details = [
        {"finding": "mod_date_after_creation_date", "severity": "high", "description": "The file was modified ...",
         "data": {"gap_seconds": 8 * 86400 + 20 * 3600 + 46 * 60, "creation_date": "2023-06-12T11:41:06+00:00",
                  "modified_date": "2023-06-21T12:27:26+04:00"}},
        {"finding": "pdf_editor_detected", "severity": "medium", "description": "...", "data": {"value": "iLovePDF"}},
        {"finding": "xmp_metadata", "severity": "info", "description": "XMP present."},
    ]
    summary = summarize_check("metadata_forensics", "completed", {"result": "flag", "details": details})
    texts = {i["title"]: i["text"] for i in summary["items"]}
    assert texts == {
        "Edited after creation": "modified 8 days 21 h after it was created (12 Jun 2023 → 21 Jun 2023)",
        "PDF editor used": "last saved by iLovePDF",
    }


def test_field_validation_lists_flags_and_condenses_passes():
    details = {
        "amount_in_words_consistency": {
            "status": "flag",
            "reason": "The amount in words, 'TWENTY THOUSAND', is 20,500.00 (read from the words), but the total in figures is 55,500.00.",
        },
        "date_in_future": {"status": "pass", "reason": "Document date 2024-01-01 is not in the future."},
        "tax_rate_consistency": {"status": "skipped", "reason": "No tax rate."},
    }
    summary = summarize_check("field_validation", "completed", {"result": "flag", "details": details})
    [item] = summary["items"]
    assert item["title"] == "Amount in words differs"
    assert item["text"] == "words say 20,500.00, the total in figures is 55,500.00"
    assert item["detail"].startswith("The amount in words")
    assert summary["notes"] == ["Passed: date.", "Nothing to compare for: tax rate."]


def test_stamp_check_moves_to_signature_detection():
    stamp = {"status": "flag", "reason": "The stamp reads 'ACME LLC', which does not name the issuer 'Other School' (similarity 20/100): the stamp may belong to another organisation."}
    checks = [
        SimpleNamespace(check_type="field_validation", status="completed", error_message=None,
                        result={"result": "flag", "details": {"stamp_issuer_consistency": stamp}}),
        SimpleNamespace(check_type="signature_stamp_detection", status="completed", error_message=None,
                        result={"result": "pass", "details": {"detected": [{"kind": "stamp", "text": "ACME LLC"}]}}),
    ]
    summaries = summarize_document_checks(checks)
    assert summaries["field_validation"]["items"] == []
    [item] = summaries["signature_stamp_detection"]["items"]
    assert item["title"] == "Stamp names another organisation"
    assert item["text"] == "stamp reads 'ACME LLC'; issuer is 'Other School' (20/100 alike)"
    assert "members" not in item  # internal; the report asks for it


def test_ghost_items_show_the_vision_guess_and_the_trace():
    details = [
        {"finding": "ghost_deleted_block", "severity": "high", "page": 1, "description": "Page 1: a block ...",
         "data": {"lines": 6, "crop_png_base64": "AAAA",
                  "vision_hint": {"kinds": ["bank details", "a note"], "kind": "bank details / a note",
                                  "confidence": "low", "answers_agree": False}}},
        {"finding": "ghost_content_scope", "severity": "info", "description": "...", "data": {"pages": [{"page": 1}]}},
    ]
    summary = summarize_check("ghost_content", "completed", {"result": "flag", "details": details})
    [item] = summary["items"]
    assert item["text"] == (
        "6 line(s) of text erased after the scan was converted, p.1 · vision guess: bank details "
        "(low confidence, answers differ)"
    )
    assert item["image_png_base64"] == "AAAA"


def test_headlines_for_pass_limited_not_applicable_and_failed():
    assert summarize_check("ghost_content", "completed", {"result": "not_applicable", "details": []})["headline"] \
        == "Not applicable — no page is a scan converted to editable text"
    limited = {"result": "limited", "details": [{"finding": "pixel_analysis_limited", "severity": "info",
                                                 "description": "...", "data": {"pages": [1]}}]}
    assert summarize_check("error_level_analysis", "completed", limited)["headline"].startswith("Limited — p.1")
    assert summarize_check("copy_move_detection", "completed", {"result": "pass", "details": []})["headline"] == "No issues found"
    assert summarize_check("copy_move_detection", "failed", None, "Boom. Trace").get("headline") == "Failed to run: Boom."


def test_risk_reason_uses_the_matching_check_line():
    summaries = {"metadata_forensics": summarize_check("metadata_forensics", "completed", {"result": "flag", "details": [
        {"finding": "pdf_editor_detected", "severity": "medium", "description": "...", "data": {"value": "iLovePDF"}},
    ]})}
    reason = {"rule_id": "metadata.pdf_editor_producer", "check_type": "metadata_forensics", "reason": "'x.pdf' was last saved ..."}
    assert risk_reason_short(reason, summaries) == ("PDF editor used", "last saved by iLovePDF")
    # No summary to match: the rule's own reason, without the file name, first sentence.
    assert risk_reason_short({"rule_id": "custom.thing", "reason": "'x.pdf': Something odd. More text."}, None) \
        == ("Thing", "Something odd.")


def test_first_sentence_keeps_decimals():
    assert first_sentence("Total 1,450.00 is wrong. Second sentence.") == "Total 1,450.00 is wrong."


def _answer(kind, heading="", words=()):
    return SimpleNamespace(kind=kind, heading=heading, legible_words=list(words), confidence="medium")


def test_vision_hint_keeps_only_what_answers_agree_on():
    hint = _agreed_hint([
        _answer("bank details", "Bank Details", ["Bank", "IBAN", "Swift"]),
        _answer("a note", "Note:", ["Note:", "the", "IBAN"]),
        _answer("a note", "Note:", ["the", "fees"]),
    ])
    assert hint["legible_words"] == ["IBAN"]  # "the" is filler; others appear once
    assert hint["heading"] == "Note:"
    assert hint["kinds"] == ["bank details", "a note"] and hint["answers_agree"] is False
    assert hint["confidence"] == "low"


def test_vision_hint_is_medium_only_when_answers_agree_and_read_something():
    agree = _agreed_hint([_answer("bank details", "", ["IBAN"])] * 3)
    assert agree["confidence"] == "medium" and agree["answers_agree"] is True
    layout_only = _agreed_hint([_answer("bank details")] * 3)
    assert layout_only["confidence"] == "low"
