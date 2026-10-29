"""
Spelling check, English only (app/services/forensics/spelling.py).

Synthetic OCR pages, with a fake language-model judge that records what it
was asked: the dictionary pass must hand it only real near-misses of common
words — not names, codes, British spellings, Arabic words, handwriting,
low-confidence reads or words the PDF's own text layer spells correctly —
and only the judge's confident "misspelling" verdicts are flagged.
"""
from __future__ import annotations

import pymupdf

from app.services.forensics.spelling import analyze_spelling
from app.services.llm_service import SpellingJudgment, SpellingVerdict
from app.services.ocr_service import OCRPage, OCRWord


def _page(text: str, page_number: int = 1, confidence: float = 0.99, **overrides: dict) -> OCRPage:
    """One OCR line of words, left to right; `overrides` maps a word to
    OCRWord attributes to change."""
    page = OCRPage(page_number=page_number)
    for i, token in enumerate(text.split()):
        word = OCRWord(token, 0.05 + i * 0.06, 0.1, 0.05, 0.02, confidence=confidence)
        for name, value in (overrides.get(token) or {}).items():
            setattr(word, name, value)
        page.words.append(word)
    return page


class FakeJudge:
    """Calls every candidate a misspelling of its first suggestion, unless
    `verdicts` says otherwise ({word: (verdict, confidence)})."""

    def __init__(self, verdicts: dict[str, tuple[str, str]] | None = None):
        self.verdicts = verdicts or {}
        self.asked: list[str] = []

    def __call__(self, document_type: str, candidates: list[dict]) -> SpellingJudgment:
        self.asked += [c["word"] for c in candidates]
        out = []
        for c in candidates:
            verdict, confidence = self.verdicts.get(c["word"], ("misspelling", "high"))
            out.append(SpellingVerdict(
                id=c["id"], reasoning="test", verdict=verdict, confidence=confidence,
                correction=c["suggestions"][0] if verdict == "misspelling" else "",
            ))
        return SpellingJudgment(verdicts=out)


def _flagged(result: dict) -> list[tuple[str, str]]:
    return [(f["data"]["text"], f["data"]["correction"]) for f in result["details"] if f["finding"] == "spelling_error"]


def test_english_typos_are_flagged_with_their_box():
    judge = FakeJudge()
    result = analyze_spelling(
        [_page("This Certifcate is awarded to the student for the academic year by the Univeristy")], judge=judge
    )
    assert result["result"] == "flag"
    assert sorted(_flagged(result)) == [("Certifcate", "certificate"), ("Univeristy", "university")]
    finding = next(f for f in result["details"] if f["finding"] == "spelling_error")
    assert finding["severity"] == "low"
    assert finding["bounding_box"]["page"] == 1 and finding["bounding_box"]["width"] > 0


def test_a_single_misspelled_word_is_shown():
    result = analyze_spelling([_page("Tuition fees recieved with thanks")], judge=FakeJudge())
    assert _flagged(result) == [("recieved", "received")]


def test_a_clean_document_passes_without_asking_the_model():
    judge = FakeJudge()
    result = analyze_spelling(
        [_page("Tax Invoice Tuition fees for the second semester Total amount payable Colour Organisation Programme")],
        judge=judge,
    )
    assert result["result"] == "pass"
    assert judge.asked == []


def test_names_codes_and_acronyms_are_not_candidates():
    judge = FakeJudge()
    fields = {"additional_fields": [{"field_name": "student_name", "value": "Ravindra Kulkarni"}]}
    analyze_spelling(
        [_page("Student Ravindra Kulkarni Grade KG2 TRN 100234567800003 ref INV-2024/0042 email a.b@school.ae GEMS iPhone")],
        extracted_fields=fields,
        judge=judge,
    )
    assert judge.asked == []


def test_words_not_near_a_common_word_are_not_candidates():
    judge = FakeJudge()
    analyze_spelling([_page("Signed by Qwertzuiop on behalf of Xylophonics")], judge=judge)
    assert judge.asked == []


def test_low_confidence_and_handwritten_words_are_skipped():
    judge = FakeJudge()
    page = _page(
        "Certifcate awarded Univeristy",
        Certifcate={"confidence": 0.6},
        Univeristy={"italic_or_handwritten": True},
    )
    result = analyze_spelling([page], judge=judge)
    assert judge.asked == [] and result["result"] == "pass"


def test_words_inside_a_stamp_are_skipped():
    judge = FakeJudge()
    page = _page("Certifcate awarded")
    stamp = {"kind": "stamp", "bounding_box": {"page": 1, "x": 0.0, "y": 0.0, "width": 0.2, "height": 0.5}}
    analyze_spelling([page], signature_regions=[stamp], judge=judge)
    assert judge.asked == []


def test_model_verdicts_other_than_a_confident_misspelling_are_set_aside():
    judge = FakeJudge({"Certifcate": ("proper_noun", "high"), "Univeristy": ("misspelling", "low")})
    result = analyze_spelling([_page("Certifcate from the Univeristy")], judge=judge)
    assert result["result"] == "pass"
    scope = next(f for f in result["details"] if f["finding"] == "spelling_scope")
    assert {s["word"] for s in scope["data"]["set_aside"]} == {"Certifcate", "Univeristy"}


def test_without_a_model_nothing_is_flagged():
    result = analyze_spelling([_page("This Certifcate is awarded")], judge=None)
    assert result["result"] == "limited"
    assert _flagged(result) == []


def test_arabic_words_are_not_checked():
    judge = FakeJudge()
    result = analyze_spelling([_page("وزارة التربية و التعليمم شهادة الطالبب")], judge=judge)
    assert judge.asked == [] and result["result"] == "not_applicable"


def test_a_mixed_english_and_arabic_document_has_its_english_checked():
    judge = FakeJudge()
    result = analyze_spelling(
        [_page("شهادة التعليمم This Certifcate is awarded to الطالبب for the academic year")], judge=judge
    )
    assert judge.asked == ["Certifcate"]
    assert _flagged(result) == [("Certifcate", "certificate")]
    scope = next(f for f in result["details"] if f["finding"] == "spelling_scope")
    assert scope["data"]["words_checked_by_language"] == {"en": 5}


def _pdf_with_text(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 80), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_an_ocr_misread_is_ruled_out_by_the_text_layer():
    # The PDF says "Certificate"; OCR read "Certifcate".
    judge = FakeJudge()
    pdf = _pdf_with_text("This Certificate is awarded to the student for the academic year")
    result = analyze_spelling(
        [_page("This Certifcate is awarded to the student for the academic year")], pdf_bytes=pdf, judge=judge
    )
    assert judge.asked == [] and result["result"] == "pass"


def test_a_typo_in_the_text_layer_itself_is_confirmed():
    pdf = _pdf_with_text("This Certifcate is awarded to the student for the academic year")
    result = analyze_spelling(
        [_page("This Certifcate is awarded to the student for the academic year")], pdf_bytes=pdf, judge=FakeJudge()
    )
    [finding] = [f for f in result["details"] if f["finding"] == "spelling_error"]
    assert finding["data"]["confirmed_by_text_layer"] is True


def test_no_text_is_not_applicable():
    assert analyze_spelling([_page("12 34 5.00")], judge=FakeJudge())["result"] == "not_applicable"


def test_summary_lists_each_word_once_with_its_correction():
    from app.services.check_summaries import risk_reason_short, summarize_check

    result = analyze_spelling(
        [_page("Recieved with thanks"), _page("Fees recieved in full", page_number=2)], judge=FakeJudge()
    )
    summary = summarize_check("spelling_consistency", "completed", result)
    assert summary["headline"] == "1 misspelled word: 'Recieved'"
    [item] = summary["items"]
    assert item["title"] == "Misspelled word"
    assert "'received'" in item["text"] and "pp. 1, 2" in item["text"]
    title, line = risk_reason_short(
        {"rule_id": "content.spelling_error", "check_type": "spelling_consistency"},
        {"spelling_consistency": summary},
    )
    assert title == "Misspelled words" and "'received'" in line


def test_summary_of_a_clean_document():
    from app.services.check_summaries import summarize_check

    result = analyze_spelling([_page("Tuition fees received with thanks")], judge=FakeJudge())
    assert summarize_check("spelling_consistency", "completed", result)["headline"] == (
        "5 printed words checked — no misspellings"
    )


def test_spelling_rule_is_seeded_low_weight():
    from app.services.risk_rule_catalog import FINDING_NAMES
    from app.services.risk_rule_seed import SEED_RULES

    rule = next(r for r in SEED_RULES if r["rule_id"] == "content.spelling_error")
    assert (rule["weight"], rule["severity"], rule["check_type"]) == (5, "low", "spelling_consistency")
    assert "spelling_error" in FINDING_NAMES["spelling_consistency"]
