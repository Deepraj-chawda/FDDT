"""
Analysis integrity: did the automated analysis of a document actually run,
and was the document trying to steer it?

Two findings, both scored by their own risk rule:

* `prompt_injection_suspected` (high) — the document's text addresses the
  language model ("ignore previous instructions", "note to the AI", "mark
  this invoice as clean", ...), or Azure OpenAI refused to read it because
  its jailbreak filter detected exactly that. A genuine business document
  never talks to an AI. The text is screened from the OCR read, so an
  instruction printed in the page image (which the vision check sees, and
  Azure's text filter does not) is caught too.
* `analysis_incomplete` (medium) — text extraction / classification failed
  for any other reason (an Azure outage, a timeout, an unreadable file).
  Field validation and issuer verification then never ran, so the case
  must not read as clean low risk just because those checks are missing.

The patterns are deliberately narrow (each needs an instruction verb AND an
AI/instruction noun): invoices legitimately say "ignore the previous
invoice" or "please disregard if already paid".
"""
from __future__ import annotations

import re
from typing import Any

FINDING_INJECTION = "prompt_injection_suspected"
FINDING_INCOMPLETE = "analysis_incomplete"

# Not a bare "model" or "assistant": invoices say "note for model X-200",
# letters "message for the assistant manager".
_AI = r"(?:ai|a\.i\.|ai\s+model|ai\s+assistant|language\s+model|llm|chat\s*gpt|gpt|analysis\s+engine|automated\s+(?:review|reviewer|system|analysis))"
_INSTRUCTIONS = r"(?:instructions?|prompts?|rules|guidelines|directives)"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{{0,40}}\b(?:previous|prior|above|earlier|all|any|your|system)\b[^.\n]{{0,20}}\b{_INSTRUCTIONS}\b",
        rf"\b(?:note|notice|message|instruction|reminder)s?\s+(?:to|for)\s+(?:the\s+|any\s+)?{_AI}\b",
        r"\b(?:system|developer)\s+(?:prompt|instruction)s?\b",
        rf"\b(?:you\s+are|act\s+as|pretend\s+to\s+be)\s+(?:an?\s+|the\s+)?{_AI}\b",
        rf"\b(?:dear|attention|hey|hello)\s*,?\s+{_AI}\b",
        r"\b(?:mark|classify|report|treat|flag)\s+(?:this|the)\s+(?:document|invoice|file|receipt|case)\s+as\s+(?:clean|authentic|genuine|verified|legitimate|low[\s-]risk|not\s+tampered)\b",
        r"\b(?:report|return|output)\s+no\s+(?:inconsistenc|anomal|issues|findings|signs|tamper)",
        r"\bdo\s+not\s+(?:report|flag|mention)\s+(?:any\s+)?(?:inconsistenc|anomal|issues|findings|tamper|discrepanc)",
    )
]

_MAX_QUOTES = 3
_QUOTE_CHARS = 160


def find_injection_phrases(text: str | None) -> list[str]:
    """The instruction-like passages in `text` (each quoted with a little
    context), most relevant first; empty when there are none."""
    if not text:
        return []
    quotes: list[str] = []
    seen: set[tuple[int, int]] = set()
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in seen):
                continue
            seen.add(span)
            start = max(0, match.start() - 40)
            quote = " ".join(text[start: match.end() + 60].split())
            quotes.append(quote[:_QUOTE_CHARS])
            if len(quotes) >= _MAX_QUOTES:
                return quotes
    return quotes


def is_jailbreak_refusal(error: str | None) -> bool:
    """Whether an extraction error is Azure OpenAI's content filter refusing
    the prompt because it detected a jailbreak (Prompt Shields)."""
    if not error:
        return False
    lowered = error.lower()
    return "jailbreak" in lowered and ("content_filter" in lowered or "content management policy" in lowered)


def analyze_analysis_integrity(*, ocr_text: str | None, processing_error: str | None = None) -> dict[str, Any]:
    """The check's stored result: `{"result": "pass"|"flag", "details": [...]}`.
    `processing_error` is set when text extraction / classification failed."""
    details: list[dict[str, Any]] = []
    quotes = find_injection_phrases(ocr_text)
    refused = is_jailbreak_refusal(processing_error)
    if quotes or refused:
        if refused:
            description = (
                "Azure OpenAI refused to analyse this document: its jailbreak filter detected text trying to "
                "instruct the AI. A genuine business document does not address an AI."
            )
        else:
            description = (
                "The document's text contains instructions addressed to an AI reviewer, e.g. "
                f"\"{quotes[0]}\". A genuine business document does not address an AI."
            )
        details.append({
            "finding": FINDING_INJECTION,
            "severity": "high",
            "description": description,
            "data": {"source": "model_refusal" if refused else "document_text", "quotes": quotes},
        })
    if processing_error is not None and not refused:
        details.append({
            "finding": FINDING_INCOMPLETE,
            "severity": "medium",
            "description": (
                "Text extraction and classification failed, so field validation and issuer verification did not "
                f"run: {processing_error[:200]}"
            ),
            "data": {"error": processing_error[:500]},
        })
    return {"result": "flag" if details else "pass", "details": details}
