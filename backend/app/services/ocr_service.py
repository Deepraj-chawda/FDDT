"""
OCR / layout extraction service — wraps Azure Document Intelligence's
Layout model (generic text/table/key-value extraction, no per-document-
type template) per SPECIFICATION.md sections 2 and 3.6. Handles printed Arabic
natively (Azure Document Intelligence's own limitation, not this code's:
handwritten Arabic is not reliably supported — SPECIFICATION.md section 3.7).

Analyzes by URL (a short-lived SAS URL from StorageService.get_download_
url), not by uploading raw bytes through this service — the Celery task
already has that URL, and Document Intelligence supports fetching from
any publicly-reachable HTTPS URL directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import settings
from app.services import rate_limiter


class OCRConfigurationError(RuntimeError):
    """Raised when the OCR backend is missing required configuration."""


class OCROperationError(RuntimeError):
    """Raised when an otherwise-configured OCR backend fails an operation."""


@dataclass
class OCRTable:
    row_count: int
    column_count: int
    # Flat list of cells; each a {"row_index", "column_index", "content"} dict
    # — kept simple rather than a 2D grid since cells can span multiple rows/
    # columns and callers here only need the text, not layout geometry.
    cells: list[dict]


@dataclass
class OCRWord:
    """One recognized word with its box as a normalized (0-1) page fraction
    — the same convention as every other bounding box in this codebase.
    `font_family` is Layout's `similarFontFamily` estimate for the word (e.g.
    "Arial, Helvetica, sans-serif") with its confidence, when the styleFont
    add-on was requested and returned one (words only, not lines)."""

    text: str
    x: float
    y: float
    width: float
    height: float
    font_family: str | None = None
    font_confidence: float | None = None
    # Also from styleFont: italic, or handwritten (signatures are usually one or the other).
    italic_or_handwritten: bool = False


@dataclass
class OCRPage:
    """`page_number` is 1-based. Geometry only — the text of the whole
    document still lives in `OCRResult.text`. This is what lets extracted
    field values be located on the page (app/services/field_locator_service.py);
    Azure Document Intelligence returns it with every Layout analysis, it
    just used to be thrown away after the text was read."""

    page_number: int
    words: list[OCRWord] = field(default_factory=list)
    lines: list[OCRWord] = field(default_factory=list)  # a line is a word-shaped span of text


@dataclass
class OCRResult:
    # Azure Document Intelligence's own concatenated text content for the
    # whole document — this is what gets sent to the LLM and what's kept in
    # documents.ocr_text for evidence/review.
    text: str
    tables: list[OCRTable] = field(default_factory=list)
    key_value_pairs: dict[str, str] = field(default_factory=dict)
    pages: list[OCRPage] = field(default_factory=list)


def _fonts_by_offset(result) -> tuple[dict[int, tuple[str, float]], set[int]]:
    """From the styleFont add-on's `styles`: content offset -> (similar font
    family, confidence), where overlapping styles keep the most confident one;
    and the offsets styled italic or handwritten."""
    best: dict[int, tuple[str, float]] = {}
    slanted: set[int] = set()
    for style in getattr(result, "styles", None) or []:
        family = getattr(style, "similar_font_family", None)
        # font_style is a str enum: compare by value (str() of it is "DocumentFontStyle.ITALIC").
        is_slanted = getattr(style, "font_style", None) == "italic" or bool(getattr(style, "is_handwritten", None))
        if not family and not is_slanted:
            continue
        confidence = float(getattr(style, "confidence", None) or 0.0)
        for span in style.spans or []:
            for offset in range(span.offset, span.offset + span.length):
                if is_slanted:
                    slanted.add(offset)
                if family and (offset not in best or best[offset][1] < confidence):
                    best[offset] = (family, confidence)
    return best, slanted


def _normalized_box(polygon: list[float] | None, page_width: float, page_height: float):
    """Azure returns a flat [x1, y1, x2, y2, ...] polygon in the page's own
    units (inches for PDFs, pixels for images); the axis-aligned bounding
    box of it, divided by the page size, is the normalized box."""
    if not polygon or len(polygon) < 4 or not page_width or not page_height:
        return None
    xs, ys = polygon[0::2], polygon[1::2]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return (
        round(max(0.0, x0 / page_width), 4),
        round(max(0.0, y0 / page_height), 4),
        round(min(1.0, (x1 - x0) / page_width), 4),
        round(min(1.0, (y1 - y0) / page_height), 4),
    )


def pages_from_result(result) -> list[OCRPage]:
    fonts, slanted = _fonts_by_offset(result)
    pages: list[OCRPage] = []
    for index, page in enumerate(result.pages or []):
        ocr_page = OCRPage(page_number=getattr(page, "page_number", None) or index + 1)
        for source, target in ((page.words, ocr_page.words), (page.lines, ocr_page.lines)):
            for item in source or []:
                text = getattr(item, "content", None)
                box = _normalized_box(getattr(item, "polygon", None), page.width, page.height)
                if text and box:
                    word = OCRWord(text, *box)
                    span = getattr(item, "span", None)
                    if target is ocr_page.words and span is not None:
                        if span.offset in fonts:
                            word.font_family, word.font_confidence = fonts[span.offset]
                        word.italic_or_handwritten = span.offset in slanted
                    target.append(word)
        pages.append(ocr_page)
    return pages


class OCRService:
    def __init__(self, endpoint: str | None, key: str | None):
        if not endpoint or not key:
            raise OCRConfigurationError(
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and "
                "AZURE_DOCUMENT_INTELLIGENCE_KEY must both be set in backend/.env "
                "(see .env.example) to enable OCR."
            )

        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        self._client = DocumentIntelligenceClient(
            endpoint=endpoint, credential=AzureKeyCredential(key)
        )

    def analyze_url(self, document_url: str) -> OCRResult:
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentAnalysisFeature
        from azure.core.exceptions import AzureError, HttpResponseError

        # Global ceiling across every extraction worker (Azure's default is
        # 15 analyze TPS) — see app/services/rate_limiter.py.
        rate_limiter.acquire(
            rate_limiter.AZURE_DOCUMENT_INTELLIGENCE,
            settings.azure_document_intelligence_max_calls_per_second,
            1.0,
        )
        try:
            features = (
                [DocumentAnalysisFeature.STYLE_FONT] if settings.azure_document_intelligence_style_font else None
            )
            poller = self._client.begin_analyze_document(
                "prebuilt-layout",
                body=AnalyzeDocumentRequest(url_source=document_url),
                features=features,
            )
            result = poller.result()
        except HttpResponseError as exc:
            if exc.status_code == 429:
                # The SDK already retried with back-off; make every worker
                # pause rather than each one keep hitting the quota.
                rate_limiter.report_throttled(
                    rate_limiter.AZURE_DOCUMENT_INTELLIGENCE, rate_limiter.retry_after_from(exc)
                )
            raise OCROperationError(f"Azure Document Intelligence request failed: {exc}") from exc
        except AzureError as exc:
            raise OCROperationError(f"Azure Document Intelligence request failed: {exc}") from exc

        tables = [
            OCRTable(
                row_count=table.row_count,
                column_count=table.column_count,
                cells=[
                    {
                        "row_index": cell.row_index,
                        "column_index": cell.column_index,
                        "content": cell.content,
                    }
                    for cell in (table.cells or [])
                ],
            )
            for table in (result.tables or [])
        ]

        key_value_pairs = {
            kv.key.content: kv.value.content
            for kv in (result.key_value_pairs or [])
            if kv.key and kv.value and kv.key.content
        }

        return OCRResult(
            text=result.content or "",
            tables=tables,
            key_value_pairs=key_value_pairs,
            pages=pages_from_result(result),
        )


    def read_image(self, image_bytes: bytes) -> tuple[str, float]:
        """Plain text read (Document Intelligence's Read model) of a small
        image, e.g. a crop: (text, mean word confidence 0-1). Used for
        best-effort readings — the faint traces of deleted text
        (app/services/forensics/ghost_content.py) — so it raises
        OCROperationError on failure for the caller to treat as no reading."""
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.exceptions import AzureError

        rate_limiter.acquire(
            rate_limiter.AZURE_DOCUMENT_INTELLIGENCE,
            settings.azure_document_intelligence_max_calls_per_second,
            1.0,
        )
        try:
            result = self._client.begin_analyze_document(
                "prebuilt-read", body=AnalyzeDocumentRequest(bytes_source=image_bytes)
            ).result()
        except AzureError as exc:
            raise OCROperationError(f"Azure Document Intelligence request failed: {exc}") from exc
        words = [w for page in result.pages or [] for w in page.words or []]
        confidence = sum(w.confidence or 0.0 for w in words) / len(words) if words else 0.0
        return (result.content or "").strip(), confidence


@lru_cache
def _build_ocr_service() -> OCRService:
    return OCRService(
        endpoint=settings.azure_document_intelligence_endpoint,
        key=settings.azure_document_intelligence_key,
    )


def get_ocr_service() -> OCRService:
    """Plain accessor (not a FastAPI dependency — called from the Celery
    task). Raises OCRConfigurationError directly; callers decide how to
    handle it."""
    return _build_ocr_service()
