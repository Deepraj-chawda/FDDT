"""
Unit tests for app/services/ocr_service.py's result mapping — no Azure call:
Layout results are built from plain dicts, the shape the SDK deserializes.
"""
from azure.ai.documentintelligence.models import AnalyzeResult

from app.services.ocr_service import pages_from_result


def _result(styles: list[dict]) -> AnalyzeResult:
    content = "Total 1,450.00 Signed"
    words = [("Total", 0), ("1,450.00", 6), ("Signed", 15)]
    return AnalyzeResult(
        {
            "apiVersion": "2024-11-30",
            "modelId": "prebuilt-layout",
            "content": content,
            "pages": [
                {
                    "pageNumber": 1,
                    "width": 8.5,
                    "height": 11,
                    "unit": "inch",
                    "spans": [{"offset": 0, "length": len(content)}],
                    "words": [
                        {
                            "content": text,
                            "polygon": [1 + i, 1, 1.8 + i, 1, 1.8 + i, 1.2, 1 + i, 1.2],
                            "span": {"offset": offset, "length": len(text)},
                            "confidence": 0.99,
                        }
                        for i, (text, offset) in enumerate(words)
                    ],
                    "lines": [],
                }
            ],
            "styles": styles,
        }
    )


def test_words_carry_the_most_confident_font_estimate_and_slant():
    result = _result(
        [
            {"similarFontFamily": "Segoe UI, sans-serif", "confidence": 0.9, "spans": [{"offset": 0, "length": 21}]},
            {"similarFontFamily": "Arial, Helvetica, sans-serif", "confidence": 0.99, "spans": [{"offset": 6, "length": 8}]},
            {"fontStyle": "italic", "confidence": 0.95, "spans": [{"offset": 15, "length": 6}]},
        ]
    )
    (page,) = pages_from_result(result)
    total, amount, signed = page.words
    assert (total.font_family, total.font_confidence, total.italic_or_handwritten) == ("Segoe UI, sans-serif", 0.9, False)
    assert (amount.font_family, amount.font_confidence) == ("Arial, Helvetica, sans-serif", 0.99)
    assert signed.italic_or_handwritten is True


def test_words_without_styles_have_no_font_estimate():
    (page,) = pages_from_result(_result([]))
    assert all(w.font_family is None and w.font_confidence is None for w in page.words)
