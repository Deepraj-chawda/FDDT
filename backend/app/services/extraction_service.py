"""
Template-free semantic field extraction (SPECIFICATION.md section 3.6): one
generic pipeline for every document type, not a per-type extractor.

Classification and extraction happen as a single LLM call
(`LLMService.classify_and_extract`) rather than two separate calls: the
model needs the same read of the document text for both, a second round
trip would double latency/cost for no accuracy benefit, and the label
list is already passed in as a parameter (app/services/
classification_service.py's DOCUMENT_TYPE_LABELS) so this stays generic
— nothing here is hardcoded per document type.
"""
from app.services.classification_service import DOCUMENT_TYPE_LABELS
from app.services.llm_service import DocumentAnalysis, LLMService


def classify_and_extract(llm_service: LLMService, document_text: str) -> DocumentAnalysis:
    return llm_service.classify_and_extract(
        document_text=document_text, document_type_labels=DOCUMENT_TYPE_LABELS
    )
