"""
Generates the font-mismatch tamper sample Sample18 from the clean Sample8 invoice.

    backend/.venv/Scripts/python.exe sample-documents/tools/make_font_mismatch_sample.py

Input : sample-documents/Sample8_English_Case_Invoice.pdf (all Helvetica)
Output: sample-documents/tampered_test_samples/Sample18_English_FontMismatch_Invoice.pdf

Line item 1 is inflated (unit price 1,150.00 -> 1,450.00) and every amount it
feeds is rewritten so the arithmetic still adds up. The original glyphs are
really removed (redaction) and the new amounts are set in Times at the same
size, colour, baseline and right edge - so the only tell is the serif font
among Helvetica, which is what the vision review's font-consistency check
should report. No raster edits: ELA / copy-move should stay quiet.
"""
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Sample8_English_Case_Invoice.pdf"
OUT = ROOT / "tampered_test_samples" / "Sample18_English_FontMismatch_Invoice.pdf"

# original span text -> replacement (same layout, Times instead of Helvetica)
EDITS = {
    "1,150.00": "1,450.00",
    "11,500.00": "14,500.00",
    "15,450.00 USD": "18,450.00 USD",
    "1,081.50 USD": "1,291.50 USD",
    "16,531.50 USD": "19,741.50 USD",
}


def main():
    doc = pymupdf.open(SRC)
    page = doc[0]
    spans = [
        s
        for b in page.get_text("dict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        if s["text"].strip() in EDITS
    ]
    assert len(spans) == len(EDITS), [s["text"] for s in spans]

    for s in spans:
        page.add_redact_annot(pymupdf.Rect(s["bbox"]), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

    for s in spans:
        new = EDITS[s["text"].strip()]
        font = "Times-Bold" if "Bold" in s["font"] else "Times-Roman"
        c = s["color"]
        rgb = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
        page.insert_text((s["origin"][0], s["origin"][1]), new, fontname=font, fontsize=s["size"], color=rgb)

    doc.save(OUT, garbage=3, deflate=True)
    print(f"wrote {OUT.relative_to(ROOT.parent)}")


if __name__ == "__main__":
    main()
