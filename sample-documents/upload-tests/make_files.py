"""Generates the files used to test upload validation by hand (in a browser).

    cd backend && .venv/Scripts/python ../sample-documents/upload-tests/make_files.py
"""
import io
import os

import pymupdf
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))


def out(name, data):
    with open(os.path.join(HERE, name), "wb") as f:
        f.write(data)


doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 72), "Upload test invoice INV-2026-001 total 1,250.00 AED")
valid = doc.tobytes()

out("valid.pdf", valid)
out("empty.pdf", b"")
out("too-big.pdf", b"%PDF-1.7\n" + b"0" * (12 * 1024 * 1024))
out("truncated.pdf", valid[: len(valid) // 2])
out("locked.pdf", pymupdf.open(stream=valid).tobytes(
    encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner"))
buf = io.BytesIO()
Image.new("RGB", (300, 200), (200, 30, 30)).save(buf, "JPEG")
out("photo.jpg", buf.getvalue())
out("photo-renamed.pdf", buf.getvalue())
out("notes.pdf", b"just plain text renamed to .pdf\n" * 10)
print("written to", HERE)
