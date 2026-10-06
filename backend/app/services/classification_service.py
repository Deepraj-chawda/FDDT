"""
Document-type classification — configurable label list (SPECIFICATION.md section
3.1: "classified against a configurable label list").

Defined once, here, and passed as a parameter into
app/services/llm_service.py's `classify_and_extract()` — not hardcoded
per call site, so adding a document type is a one-line change to this
list, not a code change scattered across the pipeline. The actual LLM
call is combined with field extraction; see
app/services/extraction_service.py for why.

A case's documents are commonly a claim-shaped pair: one document making
a claim (an invoice/quotation/bill) and one document supporting it (a
payment slip, bank confirmation, receipt, or confirmation email proving
it was actually paid/actioned). Those two shapes are genuinely different
document types, not variants of each other, so `payment_evidence` exists
as its own label rather than letting evidence documents get force-fit
into whichever invoice-shaped label is the closest loose match (that was
happening before this label existed — e.g. a payment receipt landing on
`travel_receipt` just because it was travel-related, with the model's
own confidence dropping to flag the poor fit).
"""

DOCUMENT_TYPE_LABELS: list[str] = [
    "school_document",
    "vendor_invoice",
    "commercial_invoice",
    "procurement_documentation",
    "quotation",
    "travel_invoice",
    "payment_evidence",
    "other",
]


def get_document_role(document_type: str | None) -> str:
    """Which side of the claim/evidence split a document_type falls on
    — mirrors frontend/src/types/case.ts's getDocumentRole (kept in sync
    by hand, same as DOCUMENT_TYPE_LABELS above). Used by
    app/services/cross_document_service.py: a claim document and its own
    evidence document are *expected* to disagree on issuer/date (an
    invoice's vendor vs. a bank's payment advice; the invoice date vs.
    the later payment date) in a way that isn't itself suspicious, so
    that comparison is weighted differently from a same-role mismatch."""
    if document_type is None:
        return "pending"
    if document_type == "payment_evidence":
        return "evidence"
    if document_type == "other":
        return "other"
    return "claim"
