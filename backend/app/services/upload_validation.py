"""
Upload validation — runs synchronously in the upload endpoint
(app/api/documents.py) BEFORE the file is written to Blob Storage and before
any Celery task is queued, so a bad file never costs a worker slot, a
Document Intelligence call or Azure OpenAI quota.

Only PDF documents are accepted (2026-10-03): every forensic check in the
pipeline is PDF-only, so an image upload would only ever get OCR and no
tamper analysis. Checks, in order (the first failure wins):

1. Size: empty (0 bytes) → `file_empty`; over the limit → `file_too_large`
   (states both the limit and the received size).
2. Type, from the file's own header bytes (magic number `%PDF-`) — never
   from the extension or the browser-declared MIME type. Anything else →
   `unsupported_file_type` (an image is named as such in the message, so the
   uploader knows to save/scan it as a PDF).
3. Extension agreement: a PDF whose name carries a recognised *other*
   file-type extension (`.png`, `.docx`, …) → `file_type_mismatch`. An
   unrecognised suffix (e.g. the ".2026" in "Invoice 12.03.2026") or no
   extension is accepted on the content alone.
4. Parse, with PyMuPDF (the same library the pipeline renders pages with):
   * needs a password to open → `file_password_protected`. We reject and
     ask the uploader to remove it rather than accepting a password: the
     server never handles other people's document passwords. (A PDF with
     only an *owner* password — printing/copying restrictions — opens
     without one and is accepted.)
   * cannot be parsed → `file_corrupted`. MuPDF silently "repairs" a
     truncated PDF: into zero pages (counted as corrupted), or into the
     pages that survived — so a repaired file whose "%%EOF" end marker is
     missing is also corrupted. Every page's content stream is parsed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import pymupdf

PDF_CONTENT_TYPE = "application/pdf"
ALLOWED_DESCRIPTION = "PDF"

# Extensions that clearly name some type other than PDF: a PDF carrying one
# of these is rejected as mismatched. Any other suffix is treated as part of
# the name, not a type claim.
_OTHER_TYPE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".heic",
    ".heif", ".svg", ".txt", ".csv", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".htm", ".html", ".xml", ".json", ".md", ".zip", ".rar", ".7z", ".gz", ".tar",
    ".exe", ".dll", ".msg", ".eml",
})

# Header bytes of common non-PDF uploads — used only to word the rejection
# ("this is a JPEG image"), never to accept anything.
_OTHER_SIGNATURES: tuple[tuple[bytes | tuple[bytes, ...], str], ...] = (
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"\x89PNG\r\n\x1a\n", "a PNG image"),
    ((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"), "a TIFF image"),
    ((b"GIF87a", b"GIF89a"), "a GIF image"),
    (b"BM", "a BMP image"),
    (b"PK\x03\x04", "a ZIP archive or Office document"),
    (b"\xd0\xcf\x11\xe0", "an Office document"),
)

# The PDF spec lets readers accept the "%PDF-" header anywhere in the first
# 1024 bytes (some generators prepend junk); Acrobat and MuPDF both do.
_PDF_HEADER_WINDOW = 1024
# ...and look for the "%%EOF" end marker in the last 1024 bytes.
_PDF_TAIL_WINDOW = 1024


class UploadRejected(Exception):
    """A file the upload endpoint refuses. `code` is stable for the
    frontend; `message` is shown to the user as-is."""

    def __init__(self, code: str, message: str, status_code: int, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class ValidatedUpload:
    content_type: str
    page_count: int


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def check_size(size: int, max_bytes: int) -> None:
    if size == 0:
        raise UploadRejected("file_empty", "The file is empty (0 bytes).", 400, size_bytes=0)
    if size > max_bytes:
        raise UploadRejected(
            "file_too_large",
            f"The file is {format_size(size)}; the maximum allowed size is {format_size(max_bytes)}.",
            413,
            size_bytes=size,
            max_size_bytes=max_bytes,
        )


def is_pdf(content: bytes) -> bool:
    """Whether the header bytes say PDF (the only accepted type)."""
    return b"%PDF-" in content[:_PDF_HEADER_WINDOW]


def describe_non_pdf(content: bytes) -> str | None:
    """'a JPEG image', 'a ZIP archive…' — for the rejection message only."""
    for signature, description in _OTHER_SIGNATURES:
        if content.startswith(signature):
            return description
    return None


def _check_extension(filename: str | None) -> None:
    extension = PurePosixPath(filename or "").suffix.lower()
    if extension not in _OTHER_TYPE_EXTENSIONS:
        return  # ".pdf", no extension, or a suffix that isn't a type claim
    raise UploadRejected(
        "file_type_mismatch",
        f"The file is named '{extension}' but its content is a PDF. "
        "Please upload it with the .pdf extension.",
        415,
        detected_type=PDF_CONTENT_TYPE,
        extension=extension,
    )


def _corrupted() -> UploadRejected:
    return UploadRejected(
        "file_corrupted", "The file appears to be corrupted or unreadable.", 422
    )


def _parse_pdf(content: bytes) -> int:
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - FileDataError, RuntimeError, ...
        raise _corrupted() from exc
    try:
        if doc.needs_pass:
            raise UploadRejected(
                "file_password_protected",
                "This file is password-protected. Please remove the password and re-upload.",
                422,
            )
        if doc.page_count == 0:
            raise _corrupted()  # what MuPDF "repairs" a badly truncated file into
        if doc.is_repaired and b"%%EOF" not in content[-_PDF_TAIL_WINDOW:]:
            # MuPDF rebuilt the cross-reference table AND the end-of-file
            # marker is missing: the file was cut off (it can still yield a
            # few pages, with the rest of the document silently lost). A
            # merely sloppy PDF — wrong xref offsets — still ends in %%EOF.
            raise _corrupted()
        try:
            for page in doc:
                page.get_text()  # parses the page's content stream
        except Exception as exc:  # noqa: BLE001
            raise _corrupted() from exc
        return doc.page_count
    finally:
        doc.close()


def validate_upload(content: bytes, filename: str | None, *, max_bytes: int) -> ValidatedUpload:
    """Raise UploadRejected for the first failed check, else describe the
    file. `content` is the full file (the caller has already bounded it)."""
    check_size(len(content), max_bytes)
    if not is_pdf(content):
        found = describe_non_pdf(content)
        prefix = f"This file is {found}. " if found else "This file type is not supported. "
        raise UploadRejected(
            "unsupported_file_type",
            prefix + "Only PDF files are accepted — please save or scan the document as a PDF.",
            415,
        )
    _check_extension(filename)
    return ValidatedUpload(PDF_CONTENT_TYPE, page_count=_parse_pdf(content))
