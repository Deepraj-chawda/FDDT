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


# ---------------------------------------------------------------------------
# Zips for bulk upload tests
# ---------------------------------------------------------------------------

import struct
import zipfile
import zlib


class _RawUtf8NameInfo(zipfile.ZipInfo):
    """Stores the name as UTF-8 bytes WITHOUT the UTF-8 flag, which is what
    many zip tools do (and what makes naive readers show mojibake)."""

    def _encodeFilenameFlags(self):  # noqa: N802 - zipfile's own name
        return self.filename.encode("utf-8"), self.flag_bits & ~0x800


class _CustomNameInfo(zipfile.ZipInfo):
    """Stores `raw_name` bytes as-is, with no UTF-8 flag."""

    raw_name = b""

    def _encodeFilenameFlags(self):  # noqa: N802
        return self.raw_name, self.flag_bits & ~0x800


def make_zip(entries, *, name_style: str = "flag") -> bytes:
    """`entries`: {path: bytes}; a path ending in "/" is a directory entry.
    name_style: "flag" (standard: non-ASCII names UTF-8 + flag), "raw_utf8"
    (UTF-8 bytes, no flag), "unicode_extra" (an ASCII stand-in name plus the
    Info-ZIP Unicode Path extra field holding the real UTF-8 name)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in entries.items():
            if name_style == "raw_utf8":
                info = _RawUtf8NameInfo(path)
            elif name_style == "unicode_extra" and not path.isascii():
                info = _CustomNameInfo(path)
                stand_in = path.encode("ascii", "replace")
                info.raw_name = stand_in
                utf8 = path.encode("utf-8")
                body = b"\x01" + struct.pack("<I", zlib.crc32(stand_in) & 0xFFFFFFFF) + utf8
                info.extra = struct.pack("<HH", 0x7075, len(body)) + body
            else:
                info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.endswith("/"):
                info.external_attr = 0o40775 << 16 | 0x10
                zf.writestr(info, b"")
            else:
                zf.writestr(info, data)
    return buffer.getvalue()


def make_corrupted_pdf() -> bytes:
    """Valid header, body cut off mid-file."""
    good = make_pdf("Truncated invoice", pages=3)
    return good[: len(good) // 2]
