"""Real, parseable sample files for upload tests. The upload endpoint now
opens every file locally (app/services/upload_validation.py), so arbitrary
bytes with a .pdf name are rejected; tests that just need *a* valid upload
use these instead. Different `text` gives different bytes (and hash)."""
import io

import pymupdf
from PIL import Image


def make_pdf(text: str = "Test invoice", pages: int = 1, filler: int = 0) -> bytes:
    """A valid PDF. `filler` adds roughly that many extra bytes of text, for
    tests that need files of different sizes."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} - page {i + 1}")
        if filler:
            page.insert_textbox(page.rect + (72, 100, -72, -72), "x" * filler, fontsize=2)
    data = doc.tobytes(garbage=0, deflate=False)
    doc.close()
    return data


def make_encrypted_pdf(user_password: str = "secret", owner_password: str = "owner") -> bytes:
    doc = pymupdf.open(stream=make_pdf("Encrypted invoice"), filetype="pdf")
    data = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_password, owner_pw=owner_password
    )
    doc.close()
    return data


def make_image(fmt: str = "PNG", size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


PDF = make_pdf()
