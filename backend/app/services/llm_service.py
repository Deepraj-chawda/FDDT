"""
LLM abstraction for document classification + template-free field
extraction (SPECIFICATION.md sections 3.1/3.6), plus a semantic entity-matching
fallback used by issuer verification (app/services/issuer_service.py)
when fuzzy string matching can't help (e.g. an Arabic-script extracted
name against an English-only registry entry — different scripts, not
just spelling variance).

The model is Azure OpenAI. `LLMService` exists for the same reason
`StorageService` (app/services/storage_service.py) sits in front of Azure
Blob Storage: callers (app/services/extraction_service.py,
app/services/issuer_service.py) depend on this interface, not on Azure
OpenAI directly, so another provider would be a new class, not a rewrite
of every call site.

The single `classify_and_extract` call does both classification (against a
configurable label list) and field extraction in one round trip — see
app/services/extraction_service.py for why that's combined rather than two
separate calls.

Normalization: `classify_and_extract` now normalizes amounts and dates at
the extraction boundary (rather than leaving that to downstream regex/
date-parsing code) because an LLM handles this reliably across scripts
far better than code that has to guess a source format — Arabic-Indic
numerals (٠١٢٣٤٥٦٧٨٩), Arabic date formats, and bidi-reordering artifacts
in mixed Arabic/Latin text all defeat naive parsing. `amount`/`subtotal`/
`tax_amount` come back as plain floats plus a 3-letter currency code;
`date` comes back as ISO 8601. Each of these fields also keeps the
original text in `raw_text` for audit/display, but everything downstream
(app/services/field_validation_service.py, cross_document_service.py,
the frontend) must read the normalized `value`, never re-parse `raw_text`.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.core.config import settings
from app.services import rate_limiter


class FieldValue(BaseModel):
    """A plain string field, per SPECIFICATION.md section 3.6: "Include a
    confidence/fields_uncertain flag per extracted field; low-confidence
    key fields auto-route the document to manual review instead of
    silently proceeding." Used for fields that stay as free text
    (issuer, reference_number) — see NumericFieldValue/AmountFieldValue/
    DateFieldValue for fields normalized at extraction time."""

    value: str | None
    confidence: float
    uncertain: bool


class NumericFieldValue(BaseModel):
    """A numeric field normalized at extraction time (see module
    docstring) — `value` is always a plain float (Western digits, no
    thousands separators/symbols) or null; `raw_text` keeps the original
    as seen, for audit/display only. Used for tax_rate; AmountFieldValue
    extends this with a currency code for amount/subtotal/tax_amount."""

    value: float | None
    raw_text: str | None
    confidence: float
    uncertain: bool


class AmountFieldValue(NumericFieldValue):
    currency: str | None  # ISO 4217 3-letter code, e.g. "AED", "SAR", "USD"


class DateFieldValue(BaseModel):
    """`value` is always ISO 8601 (YYYY-MM-DD) or null, regardless of the
    document's own date format/script — see module docstring."""

    value: str | None
    raw_text: str | None
    confidence: float
    uncertain: bool


class AdditionalField(BaseModel):
    """A dynamically-named field beyond the core ones — how this
    stays template-free (SPECIFICATION.md section 3.6) despite the strict JSON
    schema mode Azure OpenAI requires, which can't express an arbitrary/
    open-ended set of object keys. An array of {field_name, value, ...}
    can hold any field name a document happens to have, within a schema
    that's still fully static. Values here are NOT normalized (unlike the
    core numeric/date fields above) — they stay as free text in the
    document's own language/script."""

    field_name: str
    value: str | None
    confidence: float
    uncertain: bool


class LineItem(BaseModel):
    """One charge line, numbers copied AS PRINTED (normalized only in
    representation) — never recomputed, because the arithmetic between them
    is exactly what app/services/field_validation_service.py checks.
    `counts_toward_total` is false for lines the document itself marks as
    already paid, informational or excluded."""

    description: str | None
    quantity: float | None
    unit_price: float | None
    discount: float | None
    line_total: float | None
    line_total_raw_text: str | None
    counts_toward_total: bool
    confidence: float
    uncertain: bool


class AmountInWords(BaseModel):
    """The total written out in words ("Twenty thousand five hundred AED
    only"), if the document has one. `value` is what the WORDS say — never
    the numeric total they accompany."""

    text: str | None
    value: float | None
    confidence: float


class DocumentAnalysis(BaseModel):
    document_type: str
    document_type_confidence: float
    issuer: FieldValue
    reference_number: FieldValue
    date: DateFieldValue
    # The grand total actually due/paid (tax-inclusive, when tax applies)
    # — SPECIFICATION.md section 3.6 originally called this just "amount"; kept
    # the same key so existing storage/consumers don't need a rename,
    # its meaning is now made explicit here and in the prompt.
    amount: AmountFieldValue
    subtotal: AmountFieldValue
    tax_amount: AmountFieldValue
    tax_rate: NumericFieldValue
    additional_fields: list[AdditionalField]
    line_items: list[LineItem] = []
    amount_in_words: AmountInWords = AmountInWords(text=None, value=None, confidence=0.0)

    def core_fields_as_dict(self) -> dict[str, dict[str, Any]]:
        """The dict-of-core-fields shape stored in `documents.
        extracted_fields` (app/tasks/document_processing.py) — kept as a
        plain dict-by-name, rather than these named attributes, because
        that's what downstream consumers (field_validation_service,
        cross_document_service, document_checks' issuer lookup, and the
        frontend) read from stored JSON."""
        return {
            "issuer": self.issuer.model_dump(),
            "reference_number": self.reference_number.model_dump(),
            "date": self.date.model_dump(),
            "amount": self.amount.model_dump(),
            "subtotal": self.subtotal.model_dump(),
            "tax_amount": self.tax_amount.model_dump(),
            "tax_rate": self.tax_rate.model_dump(),
        }


class NormalizedBoundingBox(BaseModel):
    """Page-fraction (0-1) location, same convention as app/services/
    forensics/ela.py and copy_move.py's bounding boxes — deliberately
    NOT pixel coordinates, so it's meaningful at any render resolution.
    `page` itself is filled in by the caller (app/services/
    visual_inconsistency_service.py already knows which page it sent),
    not asked of the model."""

    x: float
    y: float
    width: float
    height: float


class VisualCategoryFinding(BaseModel):
    """One of the five visual-consistency categories app/services/
    visual_inconsistency_service.py asks about per page (font, alignment,
    color/contrast, resolution/sharpness, shadow/lighting). `description`/
    `confidence`/`bounding_box` are null when `consistent` is true — the
    model isn't asked to justify a non-finding."""

    consistent: bool
    description: str | None
    confidence: Literal["low", "medium", "high"] | None
    bounding_box: NormalizedBoundingBox | None


class AIGenerationAssessment(BaseModel):
    """The sixth question asked of every page alongside the five visual-
    consistency categories above — SPECIFICATION.md section 3.4's AI-generated-
    document detection, folded into this same vision call rather than a
    separate check/vendor (see app/services/visual_inconsistency_
    service.py's module docstring for why)."""

    likely_ai_generated: bool
    description: str
    confidence: Literal["low", "medium", "high"]


class PageVisualAnalysis(BaseModel):
    """One vision-model run over one rendered page image. app/services/
    visual_inconsistency_service.py calls analyze_page_visual_consistency
    TWICE per page (independent runs) and only keeps a finding that
    either agrees across both runs or was reported "high" confidence in
    at least one — a vision-model judgment can vary run-to-run, unlike
    ELA/copy-move's deterministic pixel math."""

    font_consistency: VisualCategoryFinding
    text_alignment: VisualCategoryFinding
    color_contrast_consistency: VisualCategoryFinding
    resolution_sharpness_consistency: VisualCategoryFinding
    shadow_lighting_consistency: VisualCategoryFinding
    ai_generation_assessment: AIGenerationAssessment


class EntityMatchJudgment(BaseModel):
    """Result of judge_entity_match: `matched_candidate` is one of the
    candidate strings passed in, verbatim, or null if none plausibly
    refer to the same real-world entity as `name`."""

    matched_candidate: str | None
    reasoning: str


class SignatureComparisonResult(BaseModel):
    """Result of compare_signatures — purely qualitative, no numeric score.

    `result` maps to app/models/signature_match.py's SignatureMatchResult
    enum (same four string values). Advisory language only per SPECIFICATION.md
    §2.3/§4: "visually consistent with reference on file", never "verified"
    or "confirmed". `reasoning` is the vision model's brief plain-English
    explanation, preserved verbatim in the signature_matches row for the
    reviewer to read.

    The schema places `reasoning` before `result` deliberately — structured-
    output generation fills fields in schema order, so reasoning-first
    makes the model commit its thinking before naming a verdict (same
    discipline as EntityMatchJudgment above)."""

    reasoning: str
    result: Literal["consistent", "possibly_consistent", "inconsistent", "cannot_determine"]


class DetectedSignatureRegion(BaseModel):
    """One signature/stamp region located on a page by detect_signatures_stamps.
    `bounding_box` is normalized (0-1 page fractions, top-left origin);
    the caller fills in `page`."""

    kind: Literal["signature", "stamp"]
    description: str
    # A stamp's legible text as printed (organisation, branch, P.O. box,
    # city); "" for a signature or an unreadable stamp.
    text: str = ""
    confidence: Literal["low", "medium", "high"]
    bounding_box: NormalizedBoundingBox


class PageSignatureDetection(BaseModel):
    """Result of detect_signatures_stamps for one page. `signature_expected`
    is the model's read of whether this kind of document customarily
    carries a signature or stamp (invoices, quotations, school certificates,
    etc.) — used to flag *expected-but-missing*, never mere presence."""

    signature_expected: bool
    regions: list[DetectedSignatureRegion]


class ErasedContentGuess(BaseModel):
    """A vision model's best guess at what an erased block of text was
    (app/services/forensics/ghost_content.py). A hint for the reviewer,
    never evidence."""

    kind: str  # e.g. "bank transfer details", "" when it cannot tell
    heading: str  # the heading as far as legible, "" if none
    legible_words: list[str]
    confidence: str  # "low" | "medium" | "high"


class LLMConfigurationError(RuntimeError):
    """Raised when the LLM backend is missing required configuration."""


class LLMOperationError(RuntimeError):
    """Raised when an otherwise-configured LLM backend fails a call."""


class LLMService(ABC):
    @abstractmethod
    def classify_and_extract(
        self, document_text: str, document_type_labels: list[str]
    ) -> DocumentAnalysis:
        """Classify `document_text` against `document_type_labels` and
        extract its fields, template-free (no per-type extractor)."""
        raise NotImplementedError

    @abstractmethod
    def judge_entity_match(self, name: str, candidates: list[str]) -> EntityMatchJudgment:
        """Ask whether `name` plausibly refers to the same real-world
        entity as any of `candidates` — used as a fallback by issuer
        verification (app/services/issuer_service.py) when fuzzy string
        matching scores too low to trust, which happens whenever the
        extracted name and a registry entry are in different scripts
        (e.g. a transliterated Arabic company name vs. its English
        registry entry) even though a human would recognize them as the
        same issuer."""
        raise NotImplementedError

    @abstractmethod
    def analyze_page_visual_consistency(self, image_data_uri: str) -> PageVisualAnalysis:
        """One vision-model read of one rendered document page image
        (`image_data_uri` — a base64 data: URI, see app/services/
        visual_inconsistency_service.py._encode_page_image). Used for
        SPECIFICATION.md section 3.2's secondary visual-inconsistency review AND
        section 3.4's AI-generated-content question in one call — see
        that module's docstring."""
        raise NotImplementedError

    @abstractmethod
    def compare_signatures(
        self, ref_image_data_uri: str, target_image_data_uri: str
    ) -> "SignatureComparisonResult":
        """Qualitative vision-model comparison of two signature/stamp
        image crops (both base64 data: URIs).

        Returns a SignatureComparisonResult with a four-value verdict and
        plain-English reasoning. No numeric score — SPECIFICATION.md §2.3 is
        explicit: "never a numeric score implying precision this technique
        doesn't have". Language framing must be advisory throughout:
        'visually consistent with reference on file', never 'verified'."""
        raise NotImplementedError

    def guess_erased_content(self, trace_image_data_uri: str, page_image_data_uri: str) -> "ErasedContentGuess":
        """Best guess at what a block of erased text said: the enhanced trace
        and the page with the block outlined (both base64 data: URIs). A
        reviewer's hint only. Not abstract: a backend without vision simply
        has no guess."""
        raise LLMConfigurationError("This LLM backend cannot read images.")

    @abstractmethod
    def detect_signatures_stamps(self, image_data_uri: str) -> "PageSignatureDetection":
        """Locate any handwritten signature or stamp/seal on one rendered
        page image (base64 data: URI). Presence/placement only — this is
        NOT an identity match and says nothing about authenticity."""
        raise NotImplementedError


def _string_field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
            "uncertain": {"type": "boolean"},
        },
        "required": ["value", "confidence", "uncertain"],
        "additionalProperties": False,
    }


def _date_field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": ["string", "null"],
                "description": "ISO 8601 date (YYYY-MM-DD), normalized regardless of the "
                "document's own format/script/calendar. Null if genuinely absent.",
            },
            "raw_text": {
                "type": ["string", "null"],
                "description": "The date exactly as it appears in the document, unmodified.",
            },
            "confidence": {"type": "number"},
            "uncertain": {"type": "boolean"},
        },
        "required": ["value", "raw_text", "confidence", "uncertain"],
        "additionalProperties": False,
    }


def _numeric_field_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": {"type": ["number", "null"], "description": description},
            "raw_text": {
                "type": ["string", "null"],
                "description": "The value exactly as it appears in the document, unmodified.",
            },
            "confidence": {"type": "number"},
            "uncertain": {"type": "boolean"},
        },
        "required": ["value", "raw_text", "confidence", "uncertain"],
        "additionalProperties": False,
    }


def _amount_field_schema(description: str) -> dict[str, Any]:
    schema = _numeric_field_schema(description)
    schema["properties"]["currency"] = {
        "type": ["string", "null"],
        "description": "ISO 4217 3-letter currency code, e.g. AED, SAR, USD. Null if unclear.",
    }
    schema["required"].append("currency")
    return schema


def _analysis_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "document_type": {"type": "string"},
            "document_type_confidence": {"type": "number"},
            "issuer": _string_field_schema(),
            "reference_number": _string_field_schema(),
            "date": _date_field_schema(),
            "amount": _amount_field_schema(
                "The grand total actually due/paid, tax-inclusive when tax applies, "
                "normalized to a plain number (Western digits, no separators/symbols)."
            ),
            "subtotal": _amount_field_schema(
                "The pre-tax subtotal, only if shown separately from the total. "
                "Null if the document doesn't break this out."
            ),
            "tax_amount": _amount_field_schema(
                "The tax/VAT amount, only if shown separately. Null if the document "
                "shows no tax line — never invent 0 or any other value."
            ),
            "tax_rate": _numeric_field_schema(
                "The tax/VAT rate as a plain percentage number (e.g. 15 for \"15%\"). "
                "Null if not shown."
            ),
            "additional_fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {"type": "string"},
                        "value": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "uncertain": {"type": "boolean"},
                    },
                    "required": ["field_name", "value", "confidence", "uncertain"],
                    "additionalProperties": False,
                },
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": ["string", "null"]},
                        "quantity": {"type": ["number", "null"], "description": "As printed. Null if not shown."},
                        "unit_price": {"type": ["number", "null"], "description": "As printed. Null if not shown."},
                        "discount": {
                            "type": ["number", "null"],
                            "description": "Discount amount on this line, as printed. Null if none.",
                        },
                        "line_total": {
                            "type": ["number", "null"],
                            "description": "The line's amount AS PRINTED — never recomputed, even if it is wrong.",
                        },
                        "line_total_raw_text": {
                            "type": ["string", "null"],
                            "description": "The line total exactly as it appears in the document.",
                        },
                        "counts_toward_total": {
                            "type": "boolean",
                            "description": "False only if the document marks this line as already paid, "
                            "informational or excluded from the amount due.",
                        },
                        "confidence": {"type": "number"},
                        "uncertain": {"type": "boolean"},
                    },
                    "required": [
                        "description", "quantity", "unit_price", "discount", "line_total",
                        "line_total_raw_text", "counts_toward_total", "confidence", "uncertain",
                    ],
                    "additionalProperties": False,
                },
            },
            "amount_in_words": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": ["string", "null"],
                        "description": "The amount written out in words, exactly as printed. Null if absent.",
                    },
                    "value": {
                        "type": ["number", "null"],
                        "description": "The number those WORDS spell — even if it disagrees with the numeric total.",
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["text", "value", "confidence"],
                "additionalProperties": False,
            },
        },
        "required": [
            "document_type",
            "document_type_confidence",
            "issuer",
            "reference_number",
            "date",
            "amount",
            "subtotal",
            "tax_amount",
            "tax_rate",
            "additional_fields",
            "line_items",
            "amount_in_words",
        ],
        "additionalProperties": False,
    }


def _bounding_box_schema() -> dict[str, Any]:
    return {
        "type": ["object", "null"],
        "properties": {
            "x": {"type": "number", "description": "Normalized left edge, 0-1 fraction of page width."},
            "y": {"type": "number", "description": "Normalized top edge, 0-1 fraction of page height."},
            "width": {"type": "number", "description": "Normalized width, 0-1 fraction of page width."},
            "height": {"type": "number", "description": "Normalized height, 0-1 fraction of page height."},
        },
        "required": ["x", "y", "width", "height"],
        "additionalProperties": False,
    }


def _visual_category_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "consistent": {"type": "boolean", "description": description},
            "description": {
                "type": ["string", "null"],
                "description": "The specific visual detail supporting an inconsistency finding, "
                "in plain language. Null if consistent. Never report a finding you cannot point "
                "to a specific visual detail for.",
            },
            "confidence": {
                "type": ["string", "null"],
                "enum": ["low", "medium", "high", None],
                "description": "Confidence in this finding. Null if consistent.",
            },
            "bounding_box": _bounding_box_schema(),
        },
        "required": ["consistent", "description", "confidence", "bounding_box"],
        "additionalProperties": False,
    }


def _ai_generation_assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "likely_ai_generated": {
                "type": "boolean",
                "description": "Whether this page shows signs of being AI-generated or "
                "synthetically produced, as opposed to a real scanned/photographed or "
                "digitally-authored business document.",
            },
            "description": {"type": "string", "description": "Brief reasoning for the assessment."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["likely_ai_generated", "description", "confidence"],
        "additionalProperties": False,
    }


def _page_visual_analysis_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "font_consistency": _visual_category_schema(
                "Do all text regions use consistent font, weight, and size for what should be "
                "the same field type (e.g. are all dollar amounts in the same font)?"
            ),
            "text_alignment": _visual_category_schema(
                "Does all text sit on a consistent baseline within its section? Handwritten "
                "signatures and stamps never follow text baselines; uniform skew of the whole "
                "page from scanning is not an anomaly."
            ),
            "color_contrast_consistency": _visual_category_schema(
                "Are there any regions with a noticeably different background shade, "
                "saturation, or contrast than the surrounding page, as if a patch was pasted in?"
            ),
            "resolution_sharpness_consistency": _visual_category_schema(
                "Does any region look noticeably sharper, blurrier, or more pixelated than the "
                "rest of the page? Pen signatures and stamp ink are naturally softer than print."
            ),
            "shadow_lighting_consistency": _visual_category_schema(
                "For any photographed (not purely digital) content, does lighting/shadow "
                "direction look consistent across the page?"
            ),
            "ai_generation_assessment": _ai_generation_assessment_schema(),
        },
        "required": [
            "font_consistency",
            "text_alignment",
            "color_contrast_consistency",
            "resolution_sharpness_consistency",
            "shadow_lighting_consistency",
            "ai_generation_assessment",
        ],
        "additionalProperties": False,
    }


def _entity_match_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            # `reasoning` is listed (and so generated) BEFORE
            # `matched_candidate` deliberately — structured-output
            # generation fills fields in schema order, so reasoning first
            # is what makes the model actually think before committing to
            # an answer, rather than picking a candidate up front and
            # writing a post-hoc justification that can end up
            # contradicting its own final answer (observed in testing:
            # the reasoning would correctly conclude "no match" while the
            # answer field still named a candidate, because the answer
            # had already been generated first).
            "reasoning": {"type": "string"},
            "matched_candidate": {
                "type": ["string", "null"],
                "description": "One of the candidate strings, copied verbatim, or null "
                "if none plausibly refer to the same real-world entity as `name`. Must "
                "agree with the conclusion of `reasoning` above.",
            },
        },
        "required": ["reasoning", "matched_candidate"],
        "additionalProperties": False,
    }


def _signature_comparison_json_schema() -> dict[str, Any]:
    """Schema for compare_signatures — `reasoning` listed before `result`
    (same ordering discipline as _entity_match_json_schema above: model
    commits its thinking before naming a verdict)."""
    return {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Brief plain-English explanation of the visual comparison between "
                    "the two signature/stamp images. Note specific visual similarities "
                    "or differences observed. This reasoning is shown verbatim to the "
                    "human reviewer — be factual and specific, not conclusory."
                ),
            },
            "result": {
                "type": "string",
                "enum": [
                    "consistent",
                    "possibly_consistent",
                    "inconsistent",
                    "cannot_determine",
                ],
                "description": (
                    "Your visual comparison verdict. Must agree with `reasoning`. "
                    "Use 'cannot_determine' if either image is too small, blurry, "
                    "cropped, or otherwise insufficient for meaningful comparison."
                ),
            },
        },
        "required": ["reasoning", "result"],
        "additionalProperties": False,
    }


def _erased_content_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "description": "What the erased block most likely was, in a few words (e.g. 'bank transfer details', 'a note to parents'); empty if it cannot be told.",
            },
            "heading": {"type": "string", "description": "The block's heading as far as legible; empty if none can be read."},
            "legible_words": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Words actually legible in the trace image, as printed. Do not guess or complete words.",
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["kind", "heading", "legible_words", "confidence"],
    }


def _signature_detection_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "signature_expected": {
                "type": "boolean",
                "description": (
                    "True if this kind of document (judging from this page) would "
                    "customarily carry a handwritten signature or company stamp/seal "
                    "(e.g. invoice, quotation, certificate, signed letter). False for "
                    "documents where none is normally expected (e.g. a system-generated "
                    "receipt, a booking confirmation, a continuation page of line items)."
                ),
            },
            "regions": {
                "type": "array",
                "description": "Every distinct signature or stamp/seal on the page. Empty if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["signature", "stamp"]},
                        "description": {
                            "type": "string",
                            "description": "Brief plain description of what is visible.",
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "For a stamp: every word legible on it, exactly as printed and in its own "
                                "language (organisation name, branch, P.O. box, city), around the ring and in "
                                "the centre. Empty string for a signature, or if nothing on the stamp is legible."
                            ),
                        },
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "bounding_box": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number", "description": "Normalized left edge, 0-1 fraction of page width."},
                                "y": {"type": "number", "description": "Normalized top edge, 0-1 fraction of page height."},
                                "width": {"type": "number", "description": "Normalized width, 0-1 fraction of page width."},
                                "height": {"type": "number", "description": "Normalized height, 0-1 fraction of page height."},
                            },
                            "required": ["x", "y", "width", "height"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["kind", "description", "text", "confidence", "bounding_box"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["signature_expected", "regions"],
        "additionalProperties": False,
    }


_SYSTEM_PROMPT_TEMPLATE = """You are a document analysis engine for a business-document \
authentication platform. You are given the raw OCR/layout text of one \
uploaded document (which may be in English, Arabic, or a mix of both — \
handle Arabic text natively, do not transliterate or translate free-text \
values, keep them in their own script exactly as written).

Do these things in one response:

1. Classify the document into exactly one of these types: {labels}. \
If nothing fits well, use "other". Give a confidence from 0.0 to 1.0.

2. Extract these core fields, NORMALIZING amounts and dates regardless \
of the document's source language, numeral system (Western 0-9 or \
Arabic-Indic ٠-٩), or date format — this normalization is the most \
important part of your job, since downstream code only reads the \
normalized value, never the original text:
   - issuer: the organization/person who issued the document, as free \
text in its own script (not translated).
   - reference_number: invoice/receipt/reference/ID number, as free text.
   - date: the document's own date (not a due date), normalized to ISO \
8601 (YYYY-MM-DD) in `value`. Also copy the original text as seen into \
`raw_text`. `value` must always be ISO 8601 or null — never the \
original format.
   - amount: the grand TOTAL amount actually due/paid (tax-inclusive, \
when tax applies), normalized to a plain number in `value` (Western \
digits, no currency symbols, no thousands separators). Also set \
`currency` to its 3-letter ISO 4217 code (e.g. "AED", "SAR", "USD") and \
`raw_text` to the original text as seen.
   - subtotal: the pre-tax subtotal, same value/currency/raw_text shape \
as amount, ONLY if the document shows one separately from the total. \
Set value/currency/raw_text to null if it doesn't.
   - tax_amount: the tax/VAT amount, same shape, ONLY if shown \
separately. Set value/currency/raw_text to null if the document shows \
no tax line — never invent 0 or any other value just because a total \
exists.
   - tax_rate: the tax/VAT rate as a plain percentage number (e.g. 15 \
for "15%" or "VAT (15%)"), only if shown; null otherwise.
   - Set a field's value (and raw_text, where present) to null if it is \
genuinely absent from the document — never guess.

3. Put every other clearly identifiable field (line items, addresses, \
tax IDs, terms, student/employee names, dates other than the main one, \
etc.) into `additional_fields`, each with its own short descriptive \
field_name. field_name itself is always a short English snake_case \
identifier (e.g. "reservation_number", "room_type") regardless of what \
language the document is in — only the value should be in the \
document's own language/script, and these values are NOT normalized \
(unlike the core fields above).

4. List every charge line in `line_items` (invoice/quotation lines, \
fee terms, rows of a charges table — one entry per line that carries an \
amount), with quantity, unit_price, discount and line_total as plain \
numbers (normalized like amounts) and the line total's original text in \
line_total_raw_text. A rate stated in the line text ("5,500 PER MONTH X 4 \
MONTHS = 22,000") gives unit_price 5500, quantity 4, line_total 22000. \
Set counts_toward_total false only when the document itself marks a line \
as already paid, informational or excluded from the amount due. Do not \
list the subtotal, tax or total themselves as line items. Empty list if \
the document has no charge lines.

5. If the document writes an amount out in words ("Twenty thousand five \
hundred dirhams only", "فقط خمسة آلاف درهم"), copy those words into \
amount_in_words.text and put the number THE WORDS spell into \
amount_in_words.value. All null (confidence 0) if there are none.

CRITICAL for 4 and 5: copy every number exactly as printed, even when the \
arithmetic is wrong (a line total that is not quantity x unit price, lines \
that do not add up to the total, words that disagree with the figures). \
Never correct, recompute or reconcile them — finding those discrepancies \
is what this system is for.

For every field (core and additional), set `confidence` (0.0-1.0, how \
sure you are the value is correct and genuinely present in the text) and \
`uncertain` (true if confidence is low, the text was ambiguous/garbled, \
or you are inferring rather than reading the value directly).

Base every value strictly on the document text given — never invent a \
plausible-looking value that is not actually present. When normalizing \
amounts/dates, you are converting representation only (numeral system, \
format) — never change the underlying value."""


_ENTITY_MATCH_SYSTEM_PROMPT = """You judge whether an extracted \
organization name plausibly refers to the same real-world entity as any \
name in a short candidate list — including when they're written in \
different scripts or languages (e.g. an English name vs. its Arabic \
transliteration), which pure string-similarity matching cannot detect. \
You are NOT judging spelling similarity, and you are NOT judging whether \
the two organizations are in a plausible business relationship with \
each other (e.g. a bank vs. one of its account holders, a payment \
processor vs. a merchant it processed a payment for, a landlord vs. a \
tenant) — those are two DIFFERENT entities that happen to appear \
together on the same document, not the same entity under two names. You \
are judging only: would a human who knows both scripts read `name` and \
this candidate as literally the same organization, just written \
differently?

Given `name` and a `candidates` list, return the one candidate string \
(copied verbatim) that is the same entity, or null if none of them are. \
Default to null: a false match is far worse than reporting no match, so \
only return a candidate when you're confident they're the same \
organization, not merely related, similar in industry, or mentioned in \
the same document. Briefly explain your reasoning."""


# app/services/visual_inconsistency_service.py's system prompt for the
# vision-model page read — SPECIFICATION.md sections 3.2 (secondary visual-
# inconsistency review) and 3.4 (AI-generated-content question), asked
# together in one structured call. Kept here, not in that module, so
# every LLM prompt in this codebase lives in one place next to the
# schemas that constrain its output (same reasoning as
# _SYSTEM_PROMPT_TEMPLATE/_ENTITY_MATCH_SYSTEM_PROMPT above).
_VISUAL_INCONSISTENCY_SYSTEM_PROMPT = """Examine this document page image \
carefully. You are reviewing a business document (invoice, receipt, \
school record, travel document, or similar) for signs of tampering or \
synthetic generation. For each of the following, answer explicitly:

1. Font consistency: Do all text regions use consistent font, weight, \
and size for what should be the same field type (e.g. are all dollar \
amounts in the same font)? Note any exceptions with their approximate \
location.
2. Text alignment: Does all text sit on a consistent baseline within \
its section? Note any text that appears misaligned or offset. \
Handwritten signatures and stamps never follow text baselines; uniform \
skew of the whole page from scanning is not an anomaly.
3. Color/contrast consistency: Are there any regions with a noticeably \
different background shade, saturation, or contrast than the \
surrounding page, as if a patch was pasted in?
4. Resolution/sharpness consistency: Does any region look noticeably \
sharper, blurrier, or more pixelated than the rest of the page? Pen \
signatures and rubber-stamp ink are naturally softer than printed text \
and are not an anomaly.
5. Shadow/lighting consistency: For any photographed (not purely \
digital) content, does lighting/shadow direction look consistent \
across the page?
6. AI-generation assessment: Does this page show any signs of being \
AI-generated or synthetically produced, as opposed to a real scanned/ \
photographed or digitally-authored business document?

For each of categories 1-5, answer "consistent" or describe the \
specific inconsistency and its approximate location as a normalized \
bounding box (page fraction, 0-1, top-left origin). Do NOT report a \
finding unless you can point to a specific visual detail supporting \
it — do not guess or pad out findings for the sake of having something \
to say. Rate your confidence in any flagged finding as low/medium/high. \
For category 6, always give an assessment with a confidence level, \
even if "no signs of AI generation" — briefly explain your reasoning \
either way."""


# System prompt for compare_signatures (app/services/signature_comparison_service.py).
# Advisory framing throughout — SPECIFICATION.md §2.3/§4: never "verified", "confirmed",
# or "authenticated". The four verdicts map directly to SignatureMatchResult enum
# values in app/models/signature_match.py. Kept here with the other prompts so all
# LLM prompt text lives in one place next to the schemas that constrain their output.
_SIGNATURE_COMPARISON_SYSTEM_PROMPT = """\
You are assisting a human document reviewer in comparing two signature or stamp images \
from business documents. This comparison is advisory only — it is one input to a human \
review, not an automated verification or authentication decision.

You are shown two cropped images:
- Image 1: the REFERENCE signature/stamp (previously selected by a reviewer)
- Image 2: the TARGET signature/stamp from a document being reviewed

Compare them visually and return one of four verdicts:
- "consistent": The two appear visually consistent with each other — similar stroke \
patterns, pen weight, letter forms, or stamp design elements.
- "possibly_consistent": There are some visual similarities but also differences that \
prevent a confident judgment — may be the same signer under different conditions \
(different pen, angle, speed), or may not be.
- "inconsistent": The two appear visually inconsistent — materially different stroke \
patterns, letter forms, or design elements that suggest different signers or stamps.
- "cannot_determine": The images are too small, blurry, cropped, overexposed, or \
otherwise insufficient for a meaningful visual comparison.

Important guidance:
- Do NOT use language like "verified", "authenticated", or "confirmed". Your output \
is advisory for a human reviewer, not a forensic conclusion.
- Handwritten signatures naturally vary — the same person can sign differently. Note \
observable similarities AND differences rather than treating any difference as disqualifying.
- For stamps, focus on the design elements (text, logo, border) rather than ink spread.
- Be specific about what you observe (e.g. "the letter 'R' shows a similar loop \
structure", "the horizontal stroke weight differs significantly").\
"""

# System prompt for detect_signatures_stamps. Presence/placement only —
# SPECIFICATION.md §2.3: signature/stamp detection is assistive, never an
# identity or authenticity verdict.
_ERASED_CONTENT_SYSTEM_PROMPT = """You are assisting a human document reviewer. A block of text was erased from a scanned document after it was converted to editable text; only a faint trace of it remains. You get two images: (1) the trace of the erased block, contrast-stretched; (2) a tiny layout thumbnail of the whole page with the erased block filled in teal, only to show WHERE on the page it was.

Say what the block most likely was, from the trace's own shape (a heading, label/value rows, a paragraph) and its position (e.g. under a fee table, at the foot of the page). Heading and legible_words must come ONLY from image 1, the trace; the page's other text is not part of the block. Read only what is legible: never invent names, numbers or account details, and leave fields empty rather than guess. Confidence "high" only when words in the trace are clearly legible; "low" when the guess rests on layout alone."""

_SIGNATURE_DETECTION_SYSTEM_PROMPT = """\
You are assisting a human document reviewer. Look at this business-document page image \
and locate any handwritten signature and any stamp or seal (company stamp, round seal, \
rubber stamp, embossed-looking mark).

Rules:
- Report only marks that are actually visible. Do not report printed typed names, \
signature *lines* with nothing on them, logos, letterheads, or QR codes.
- The image carries a thin red reference grid: lines every 0.1 of the page's width and \
height, labeled 0.1-0.9 along the top edge (x) and left edge (y). Use it to read \
coordinates off the image rather than estimating them. The grid is an overlay, not part \
of the document — ignore it when deciding what is a signature or stamp.
- For each region give a TIGHT normalized bounding box (0-1 fractions of the FULL page's \
width/height, top-left origin: x/y is the top-left corner) that fully contains the mark \
and little else.
- For a stamp, transcribe the text you can actually read on it, exactly as printed (do not guess or complete illegible words). Leave it empty for a signature.
- Also say whether a document like this would customarily carry a signature or stamp.
- This is presence and placement only. You are not verifying whose signature it is or \
whether it is genuine — do not comment on authenticity.\
"""

# Known vision-capable Azure OpenAI deployment name families — gpt-4o
# and gpt-4.1 (and their -mini variants) all support image input per
# Azure OpenAI's documented multimodal support. This is a NAME check
# against the configured deployment, not a live capability probe (no
# API call this cheap/reliable exists to ask "can you see images?")
# — if AZURE_OPENAI_DEPLOYMENT_NAME is ever changed to something outside
# this list, __init__ raises rather than silently sending images to a
# text-only model and getting back nonsense/errors at check-run time.
_VISION_CAPABLE_DEPLOYMENT_NAME_MARKERS = ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-4-vision")


# ---------------------------------------------------------------------------
# TPM reservation estimate (feeds the token-weighted rate limiter)
# ---------------------------------------------------------------------------
# Azure counts a request against the deployment's tokens-per-minute quota as
# its prompt tokens + the full max_tokens reservation, when it arrives. The
# max_tokens part is exact; the prompt part is estimated from the request:
#
#   prompt ≈ text_chars / CHARS_PER_TOKEN
#          + Σ_images (IMAGE_BASE_TOKENS + IMAGE_TOKENS_PER_PATCH × patches)
#          + PROMPT_OVERHEAD_TOKENS
#
# where `patches` is the number of 32×32-pixel patches the image is billed as
# (capped at 1,536, the image being scaled down to fit — how gpt-4.1-family
# vision models count image input). A full page and a small signature crop
# therefore cost very differently, which a flat per-image guess can't express
# (it under-reserved full pages by ~46% and over-reserved crops by ~2.5×).
#
# The three constants are CALIBRATED against real `usage.prompt_tokens` from
# this deployment (scripts/calibrate_llm_tokens.py; every live call also logs
# its usage — see `_record_usage`). Re-run the calibration whenever the model,
# the page render size or the prompts change. A safety margin is applied on
# top so an estimate errs on the side of reserving slightly too much.
# Calibrated 2026-10-03 against gpt-4.1-mini on the 12 sample documents
# (English + Arabic; 96 real calls across all five call types):
#  - text: 1.93 chars/token — the most token-dense observed (Arabic-heavy
#    classification); English-only prompts run ~3.3-3.6, so they are
#    reserved a little high, never low.
#  - images: 1.62 tokens per 32-px patch, no per-image base — matched real
#    usage within -5%/+10% for full pages (1,536 patches) and crops alike.
#  - 5% margin: with these values every measured call was reserved at
#    >= 1.05x its real prompt tokens (old flat estimate: 0.54x for visual
#    review, 0.64x for classification — i.e. it under-reserved).
CHARS_PER_TOKEN = 1.93
IMAGE_BASE_TOKENS = 0
IMAGE_TOKENS_PER_PATCH = 1.62
PROMPT_OVERHEAD_TOKENS = 0
ESTIMATE_SAFETY_MARGIN = 1.05

# Output reservations (max_tokens) per call type. Azure counts the whole
# reservation against TPM, so it is sized from measured outputs with ample
# headroom — not left at a generic 4,000. Measured max completion on the
# sample set: entity match 144, visual review 204, signature detection 144,
# signature comparison 47, classification+extraction 1,207 (classification
# keeps 4,000: its output grows with the number of fields on a document).
MAX_TOKENS_CLASSIFICATION = 4000
MAX_TOKENS_ENTITY_MATCH = 512
MAX_TOKENS_VISUAL_REVIEW = 640
MAX_TOKENS_SIGNATURE_DETECTION = 640
MAX_TOKENS_SIGNATURE_COMPARISON = 320
MAX_TOKENS_ERASED_CONTENT = 300

_MAX_PATCHES = 1536
_PATCH = 32


def _image_size(data_uri: str) -> tuple[int, int] | None:
    """(width, height) of a base64 PNG/JPEG data URI, read from its header."""
    import base64
    import struct

    try:
        b64 = data_uri.split(",", 1)[1]
        head = base64.b64decode(b64[:4096] + "=" * (-len(b64[:4096]) % 4))
    except Exception:  # noqa: BLE001
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", head[16:24])
        return width, height
    if head[:2] == b"\xff\xd8":  # JPEG: walk to the first SOFn marker
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            length = struct.unpack(">H", head[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", head[i + 5 : i + 9])
                return width, height
            i += 2 + length
    return None


def image_patches(width: int, height: int) -> int:
    """32-px patches an image is billed as, after scaling down to fit 1,536."""
    import math

    patches = math.ceil(width / _PATCH) * math.ceil(height / _PATCH)
    if patches > _MAX_PATCHES:
        scale = math.sqrt(_MAX_PATCHES * _PATCH * _PATCH / (width * height))
        width, height = width * scale, height * scale
        patches = min(_MAX_PATCHES, math.ceil(width / _PATCH) * math.ceil(height / _PATCH))
    return patches


def _prompt_shape(
    system_prompt: str, user_content: "str | list[dict[str, Any]]"
) -> tuple[int, int, int]:
    """(text characters, image count, total image patches) of a prompt.
    An image whose size can't be read counts as the maximum 1,536 patches."""
    text_chars = len(system_prompt)
    images = patches = 0
    if isinstance(user_content, str):
        text_chars += len(user_content)
    else:
        for part in user_content:
            if part.get("type") == "text":
                text_chars += len(part.get("text") or "")
            elif part.get("type") == "image_url":
                images += 1
                size = _image_size((part.get("image_url") or {}).get("url", ""))
                patches += image_patches(*size) if size else _MAX_PATCHES
    return text_chars, images, patches


def _estimate_prompt_tokens(system_prompt: str, user_content: "str | list[dict[str, Any]]") -> int:
    text_chars, images, patches = _prompt_shape(system_prompt, user_content)
    raw = (
        text_chars / CHARS_PER_TOKEN
        + images * IMAGE_BASE_TOKENS
        + patches * IMAGE_TOKENS_PER_PATCH
        + PROMPT_OVERHEAD_TOKENS
    )
    return int(raw * ESTIMATE_SAFETY_MARGIN) + 1


def _estimate_request_tokens(
    system_prompt: str, user_content: "str | list[dict[str, Any]]", max_tokens: int
) -> int:
    """What Azure will charge this request against the deployment's TPM:
    estimated prompt tokens plus the max_tokens reservation."""
    return _estimate_prompt_tokens(system_prompt, user_content) + max_tokens


_USAGE_KEY = "fddt:llm_usage:{schema}"
_USAGE_SAMPLES_KEY = "fddt:llm_usage:{schema}:samples"


def _record_usage(
    schema_name: str,
    usage: Any,
    *,
    system_prompt: str,
    user_content: "str | list[dict[str, Any]]",
    max_tokens: int,
) -> None:
    """Log the real token usage of one call next to what the limiter
    reserved, per call type, so the estimate can be calibrated against
    reality. Never raises."""
    import logging

    try:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        text_chars, images, patches = _prompt_shape(system_prompt, user_content)
        estimated = _estimate_prompt_tokens(system_prompt, user_content)
        logging.getLogger("fddt.llm_usage").info(
            "llm_usage schema=%s prompt_tokens=%d completion_tokens=%d estimated_prompt_tokens=%d "
            "max_tokens=%d text_chars=%d images=%d image_patches=%d",
            schema_name, prompt, completion, estimated, max_tokens, text_chars, images, patches,
        )
        client = rate_limiter._redis()
        pipe = client.pipeline()
        key = _USAGE_KEY.format(schema=schema_name)
        pipe.hincrby(key, "calls", 1)
        pipe.hincrby(key, "prompt_tokens", prompt)
        pipe.hincrby(key, "completion_tokens", completion)
        pipe.hincrby(key, "estimated_prompt_tokens", estimated)
        sample = json.dumps(
            {"p": prompt, "c": completion, "e": estimated, "m": max_tokens, "t": text_chars, "i": images, "x": patches}
        )
        pipe.lpush(_USAGE_SAMPLES_KEY.format(schema=schema_name), sample)
        pipe.ltrim(_USAGE_SAMPLES_KEY.format(schema=schema_name), 0, 999)
        pipe.execute()
    except Exception:  # noqa: BLE001
        pass


class AzureOpenAILLMService(LLMService):
    def __init__(
        self,
        api_key: str | None,
        endpoint: str | None,
        deployment_name: str | None,
        api_version: str,
        request_timeout_seconds: float,
    ):
        if not api_key or not endpoint or not deployment_name:
            raise LLMConfigurationError(
                "AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, and "
                "AZURE_OPENAI_DEPLOYMENT_NAME must all be set in backend/.env "
                "(see .env.example) to enable document classification/extraction."
            )

        from openai import AzureOpenAI  # deferred: keep this import Azure-specific

        self._deployment_name = deployment_name
        self._is_vision_capable = any(
            marker in deployment_name.lower() for marker in _VISION_CAPABLE_DEPLOYMENT_NAME_MARKERS
        )
        self._client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=self._normalize_endpoint(endpoint),
            api_version=api_version,
            # See settings.azure_openai_request_timeout_seconds — the
            # SDK's own default (~10 min, 2 retries) lets one hung call
            # starve every other queued Celery task on this project's
            # single-worker dev setup.
            timeout=request_timeout_seconds,
        )

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """Accepts either the classic Azure OpenAI resource endpoint
        (https://<resource>.openai.azure.com/) or an Azure AI Foundry
        project endpoint (https://<resource>.services.ai.azure.com/api/
        projects/<name>) as copied from the Foundry portal. The chat-
        completions API (and the openai SDK's URL construction) needs the
        bare https://<resource>/ origin — anything past the host is a
        Foundry-portal-specific path the SDK doesn't expect, so it's
        stripped rather than requiring the user to hand-edit the value."""
        parts = urlsplit(endpoint)
        return f"{parts.scheme}://{parts.netloc}/"

    def _chat_json(
        self,
        system_prompt: str,
        # A plain string for classify_and_extract/judge_entity_match's
        # text-only calls, or a content-part list (text + image_url
        # parts) for analyze_page_visual_consistency's vision call —
        # both are valid `messages[].content` shapes per the chat
        # completions API.
        user_content: str | list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
        *,
        max_tokens: int = 4000,
        temperature: float = 0,
    ) -> dict[str, Any]:
        from openai import APIError, RateLimitError

        # Global requests- AND tokens-per-minute ceilings across every worker
        # for this deployment — see app/services/rate_limiter.py. Azure counts
        # the prompt plus the full max_tokens reservation against TPM.
        rate_limiter.acquire(
            rate_limiter.AZURE_OPENAI, settings.azure_openai_max_requests_per_minute, 60.0
        )
        rate_limiter.acquire(
            rate_limiter.AZURE_OPENAI_TOKENS,
            settings.azure_openai_max_tokens_per_minute,
            60.0,
            cost=_estimate_request_tokens(system_prompt, user_content, max_tokens),
            cooldown_name=rate_limiter.AZURE_OPENAI,
        )
        try:
            response = self._client.chat.completions.create(
                model=self._deployment_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except RateLimitError as exc:
            # The SDK already retried honoring Retry-After; pause all workers.
            rate_limiter.report_throttled(rate_limiter.AZURE_OPENAI, rate_limiter.retry_after_from(exc))
            raise LLMOperationError(f"Azure OpenAI request failed: {exc}") from exc
        except APIError as exc:
            raise LLMOperationError(f"Azure OpenAI request failed: {exc}") from exc

        _record_usage(
            schema_name, getattr(response, "usage", None),
            system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMOperationError("Azure OpenAI returned an empty response")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMOperationError(f"Azure OpenAI returned invalid JSON: {exc}") from exc

    def classify_and_extract(
        self, document_text: str, document_type_labels: list[str]
    ) -> DocumentAnalysis:
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(labels=", ".join(document_type_labels))
        parsed = self._chat_json(
            system_prompt, document_text, "document_analysis", _analysis_json_schema(),
            max_tokens=MAX_TOKENS_CLASSIFICATION,
        )
        try:
            return DocumentAnalysis.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc

    def judge_entity_match(self, name: str, candidates: list[str]) -> EntityMatchJudgment:
        if not candidates:
            return EntityMatchJudgment(matched_candidate=None, reasoning="No candidates to compare.")

        user_content = json.dumps({"name": name, "candidates": candidates}, ensure_ascii=False)
        parsed = self._chat_json(
            _ENTITY_MATCH_SYSTEM_PROMPT, user_content, "entity_match", _entity_match_json_schema(),
            max_tokens=MAX_TOKENS_ENTITY_MATCH,
        )
        try:
            return EntityMatchJudgment.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc

    def analyze_page_visual_consistency(self, image_data_uri: str) -> PageVisualAnalysis:
        if not self._is_vision_capable:
            raise LLMConfigurationError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME ({self._deployment_name!r}) is not recognized as "
                "a vision-capable model (known families: gpt-4o, gpt-4.1, gpt-4-turbo, "
                "gpt-4-vision, and their -mini variants). The visual inconsistency review check "
                "needs a deployment that accepts image input — deploy one of those model "
                "families under this name, or point AZURE_OPENAI_DEPLOYMENT_NAME at an existing "
                "vision-capable deployment, in backend/.env."
            )

        parsed = self._chat_json(
            _VISUAL_INCONSISTENCY_SYSTEM_PROMPT,
            [
                {"type": "text", "text": "Analyze this document page image per the instructions."},
                {"type": "image_url", "image_url": {"url": image_data_uri, "detail": "high"}},
            ],
            "page_visual_analysis",
            _page_visual_analysis_json_schema(),
            max_tokens=MAX_TOKENS_VISUAL_REVIEW,
            # Deliberately NOT 0, unlike every other _chat_json call in
            # this class. app/services/visual_inconsistency_service.py
            # calls this TWICE per page specifically to catch a one-off
            # hallucinated finding by seeing whether it recurs — at
            # temperature=0 the two runs would come back near-identical
            # (Azure OpenAI is only approximately deterministic at
            # temperature=0 due to batched-inference floating-point
            # effects), which would defeat the point of sampling twice.
            # A moderate temperature makes the two runs genuinely
            # independent samples of the model's judgment.
            temperature=0.4,
        )
        try:
            return PageVisualAnalysis.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc

    def compare_signatures(
        self, ref_image_data_uri: str, target_image_data_uri: str
    ) -> SignatureComparisonResult:
        """Qualitative comparison of two signature/stamp image crops via the
        vision model. Advisory language only — no numeric score, no "verified"
        framing (SPECIFICATION.md §2.3/§4). See _signature_comparison_json_schema()
        for the four-verdict enum and _SIGNATURE_COMPARISON_SYSTEM_PROMPT for
        the prompt framing."""
        if not self._is_vision_capable:
            raise LLMConfigurationError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME ({self._deployment_name!r}) is not a "
                "vision-capable model. Signature comparison requires image input."
            )

        parsed = self._chat_json(
            _SIGNATURE_COMPARISON_SYSTEM_PROMPT,
            [
                {
                    "type": "text",
                    "text": (
                        "Compare these two signature/stamp images and return your assessment."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": ref_image_data_uri, "detail": "high"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": target_image_data_uri, "detail": "high"},
                },
            ],
            "signature_comparison",
            _signature_comparison_json_schema(),
            max_tokens=MAX_TOKENS_SIGNATURE_COMPARISON,
            temperature=0,
        )
        try:
            return SignatureComparisonResult.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc


    def guess_erased_content(self, trace_image_data_uri: str, page_image_data_uri: str) -> ErasedContentGuess:
        if not self._is_vision_capable:
            raise LLMConfigurationError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME ({self._deployment_name!r}) is not a "
                "vision-capable model; it cannot read the erased-text trace."
            )
        parsed = self._chat_json(
            _ERASED_CONTENT_SYSTEM_PROMPT,
            [
                {"type": "text", "text": "What did the erased block say? Image 1: the trace. Image 2: the page."},
                {"type": "image_url", "image_url": {"url": trace_image_data_uri, "detail": "high"}},
                {"type": "image_url", "image_url": {"url": page_image_data_uri, "detail": "low"}},
            ],
            "erased_content",
            _erased_content_json_schema(),
            max_tokens=MAX_TOKENS_ERASED_CONTENT,
            # Asked several times (app/services/forensics/ghost_content.py
            # keeps what the answers agree on): the samples must be independent.
            temperature=0.4,
        )
        try:
            return ErasedContentGuess.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc

    def detect_signatures_stamps(self, image_data_uri: str) -> PageSignatureDetection:
        if not self._is_vision_capable:
            raise LLMConfigurationError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME ({self._deployment_name!r}) is not a "
                "vision-capable model. Signature/stamp detection requires image input."
            )

        parsed = self._chat_json(
            _SIGNATURE_DETECTION_SYSTEM_PROMPT,
            [
                {"type": "text", "text": "Locate any signature or stamp on this page."},
                {"type": "image_url", "image_url": {"url": image_data_uri, "detail": "high"}},
            ],
            "signature_detection",
            _signature_detection_json_schema(),
            max_tokens=MAX_TOKENS_SIGNATURE_DETECTION,
            temperature=0,
        )
        try:
            return PageSignatureDetection.model_validate(parsed)
        except ValueError as exc:
            raise LLMOperationError(
                f"Azure OpenAI response did not match the expected schema: {exc}"
            ) from exc


_llm_service_singleton: LLMService | None = None


def get_llm_service() -> LLMService:
    """Plain accessor (not a FastAPI dependency — this is called from the
    Celery task, which has no request/DI context). Raises
    LLMConfigurationError directly; callers decide how to handle it."""
    global _llm_service_singleton
    if _llm_service_singleton is None:
        _llm_service_singleton = AzureOpenAILLMService(
            api_key=settings.azure_openai_key,
            endpoint=settings.azure_openai_endpoint,
            deployment_name=settings.azure_openai_deployment_name,
            api_version=settings.azure_openai_api_version,
            request_timeout_seconds=settings.azure_openai_request_timeout_seconds,
        )
    return _llm_service_singleton
