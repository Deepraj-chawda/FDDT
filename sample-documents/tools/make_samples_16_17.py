"""
Regenerates the tamper-test samples Sample16 (edited) and Sample17 (all-checks-flagged).

    backend/.venv/Scripts/python.exe sample-documents/tools/make_samples_16_17.py

Inputs : sample-documents/tampered_test_samples/originals/Sample16_*_orig.pdf
Outputs: sample-documents/tampered_test_samples/Sample16_*.pdf, Sample17_*.pdf

Every edit is an INCREMENTAL save on top of the source PDF, so the original
bytes stay in the file (that is itself one of the metadata findings). The
page is covered with a JPEG copy of its own render (uniform compression
history, like an exported/scanned page), and the "edited" areas are pasted on
top as lossless patches — that is what ELA and copy-move pick up. The vector
text underneath is kept in sync with what is drawn, so OCR / text extraction
sees the same content as the eye. See SAMPLES_16_17_EXPECTED_REPORT.md.
"""
import io
import shutil
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "tampered_test_samples"
ORIG = ROOT / "originals"
DPI = 200
Z = DPI / 72
R = pymupdf.Rect
WHITE = (1, 1, 1)


# --- raster helpers -------------------------------------------------------
def render(page):
    pix = page.get_pixmap(dpi=DPI, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def crop(img, r):
    return img.crop(tuple(int(v * Z) for v in (r.x0, r.y0, r.x1, r.y1)))


def _bytes(img, fmt, **kw):
    b = io.BytesIO()
    img.save(b, fmt, **kw)
    return b.getvalue()


def noisy(img, sigma, seed):
    a = np.asarray(img).astype(np.float32)
    a += np.random.default_rng(seed).normal(0, sigma, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def bw(img, t=160):
    return img.convert("L").point(lambda v: 0 if v < t else 255).convert("RGB")


def tamper_raster(page, ela_rect, clone_src, clone_dst, *, jpeg_q=50, sigma=20):
    """Flatten the page to a JPEG, then paste (a) a noisier lossless patch over
    `ela_rect` (ELA) and (b) the same high-contrast block at `clone_src` and
    `clone_dst` (copy-move)."""
    base = render(page)
    page.insert_image(page.rect, stream=_bytes(base, "JPEG", quality=jpeg_q), overlay=True)
    patch = noisy(crop(base, R(*ela_rect)), sigma, 3)
    page.insert_image(R(*ela_rect), stream=_bytes(patch, "PNG"), overlay=True)
    block = bw(crop(base, R(*clone_src)))
    for dst in (clone_src, clone_dst):
        page.insert_image(R(*dst), stream=_bytes(block, "PNG"), overlay=True)


# --- text edit helper (real edit of the vector text, not an overlay) -------
def replace_text(page, old, new, *, grow_left=0, grow_right=0, align=0, size=None):
    hits = page.search_for(old)
    for r in hits:
        box = R(r.x0 - grow_left, r.y0 - 1, r.x1 + grow_right, r.y1 + 1)
        page.add_redact_annot(box, text=new, fontname="helv", fontsize=size or (r.height * 0.85),
                              align=align, fill=WHITE, text_color=(0.1, 0.1, 0.1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    return len(hits)


def save_incr(doc):
    doc.saveIncr()
    doc.close()


def start(src, dst):
    shutil.copy(src, dst)
    return pymupdf.open(dst)


# --- Sample16: original content + ELA + copy-move --------------------------
def make16(kind):
    name = f"Sample16_English_Case_{kind}"
    dst = ROOT / f"{name}.pdf"
    doc = start(ORIG / f"{name}_orig.pdf", dst)
    if kind == "Invoice":
        tamper_raster(doc[0], (330, 420, 548, 445), (36, 264, 300, 302), (36, 610, 300, 648))
    else:
        tamper_raster(doc[0], (50, 130, 230, 160), (40, 205, 300, 250), (40, 640, 300, 685))
    save_incr(doc)


# --- Sample17: every check flagged -----------------------------------------
def make17(kind):
    src = ORIG / f"Sample16_English_Case_{kind}_orig.pdf"
    dst = ROOT / f"Sample17_English_AllFlags_{kind}.pdf"
    doc = start(src, dst)
    page = doc[0]

    if kind == "Invoice":
        # future invoice date + due date  -> date_in_future
        replace_text(page, "Date: August 25, 2026", "Date: December 25, 2026", grow_left=10, align=2, size=7)
        replace_text(page, "Payment Due: September 24, 2026", "Payment Due: January 24, 2027", align=2, size=7)
        replace_text(page, "Managing Director | Date: August 25, 2026", "Managing Director | Date: December 25, 2026",
                     grow_left=22, grow_right=22, align=1, size=6.5)
        # malformed reference number      -> reference_number_format
        replace_text(page, "INVOICE #: INV-NGT-2026-1049", "INVOICE #: INV-NGT-2026-10!49", grow_left=8, align=2, size=8)
        replace_text(page, "payments quoting invoice # INV-NGT-2026-1049.", "payments quoting invoice # INV-NGT-2026-10!49.",
                     grow_right=10, size=6.5)
        # line 2 total edited: line items now sum to 190,000 vs subtotal 180,000
        replace_text(page, "36,000.00", "46,000.00", align=2, size=9)
        # signature swapped for a visibly different one -> signature comparison
        page.delete_image(35)
        pts = [(378, 505), (388, 480), (398, 506), (408, 480), (420, 506), (432, 484), (446, 508), (474, 490)]
        page.draw_polyline([pymupdf.Point(*p) for p in pts], color=(0.05, 0.05, 0.35), width=2.4)
    else:
        # stamp and signature removed -> signature_stamp_detection (expected but missing)
        page.delete_image(19)
        page.delete_image(23)
    save_incr(doc)  # revision 2

    doc = pymupdf.open(dst)
    if kind == "Invoice":
        tamper_raster(doc[0], (330, 420, 548, 445), (36, 264, 300, 302), (36, 610, 300, 648))
    else:
        tamper_raster(doc[0], (50, 130, 230, 160), (40, 205, 300, 250), (40, 640, 300, 685))
    doc.set_metadata({
        "title": doc.metadata.get("title") or "",
        "author": "Audited Entity",
        "creator": "Adobe Photoshop 2024 (Windows)",
        "producer": "Adobe Photoshop 2024 (Windows)",
        "creationDate": "D:20260801090000Z",
        "modDate": "D:20260902164500Z",
    })
    save_incr(doc)  # revision 3


if __name__ == "__main__":
    for k in ("Invoice", "Evidence"):
        make16(k)
        make17(k)
        print("wrote Sample16 /", "Sample17 ", k)
