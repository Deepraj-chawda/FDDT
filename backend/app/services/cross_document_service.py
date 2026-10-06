"""
Cross-document consistency validation (SPECIFICATION.md section 3.1,
"Cross-document validation & reconciliation") — pairwise comparison of
shared fields (amount, date, issuer/vendor) across every completed
document in a case. Case-level, not per-document: see app/tasks/
document_checks.py for when this runs and how results are persisted
(one `cross_document_findings` row per mismatched field pair, not a
`document_checks` row — that table has no case-level concept).

Amounts and dates are normalized at extraction time (app/services/
llm_service.py — `amount.value` is a plain float with a separate
`currency` code, `date.value` is ISO 8601), so this compares those
normalized values directly rather than re-parsing raw extracted text —
this is what makes an invoice's total reliably comparable against its
evidence document's confirmed amount even when the two used different
numeral systems or currency formatting (e.g. an Arabic-Indic-numeral
invoice vs. a Western-numeral payment receipt).

Severity is NOT uniform across fields: `amount` is the actual
reconciliation signal this whole check exists for (does the payment
match the invoice total?), so an amount mismatch is always `high`. A
claim document and its own evidence document, by contrast, are EXPECTED
to disagree on issuer (an invoice's vendor vs. a bank/payment
processor's name) and date (the invoice date vs. a later payment date)
— see app/services/classification_service.py's get_document_role — so
those mismatches are real signal only between two documents of the same
role (two claims, or two evidence documents); between a claim and its
evidence they're downgraded to `low` rather than dropped entirely,
keeping them visible for a reviewer without letting expected noise
crowd out the fields that actually matter.

Pure function: takes already-loaded Document ORM objects and returns
plain finding dicts — no DB access, no Celery/session concerns, so this
is unit-testable without a database.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from rapidfuzz import fuzz, utils

from app.models.cross_document_finding import FindingSeverity
from app.models.document import Document
from app.services.classification_service import get_document_role
from app.services.field_validation_service import _parse_date

# Below this rapidfuzz similarity score, two issuer/vendor name strings
# are treated as genuinely different rather than OCR/formatting noise
# (e.g. "Acme LLC" vs. "Acme L.L.C.").
_ISSUER_NAME_SIMILARITY_THRESHOLD = 85.0


def _core_field(document: Document, field: str) -> dict[str, Any]:
    core_fields = (document.extracted_fields or {}).get("core_fields") or {}
    return core_fields.get(field) or {}


def _amount_mismatch_description(doc_a: Document, doc_b: Document) -> str | None:
    amount_a, currency_a = _core_field(doc_a, "amount").get("value"), _core_field(doc_a, "amount").get(
        "currency"
    )
    amount_b, currency_b = _core_field(doc_b, "amount").get("value"), _core_field(doc_b, "amount").get(
        "currency"
    )
    if amount_a is None or amount_b is None:
        return None  # nothing to compare if either side didn't extract an amount

    if currency_a and currency_b and currency_a != currency_b:
        return (
            f"Amount currency differs between '{doc_a.original_filename}' "
            f"({amount_a:.2f} {currency_a}) and '{doc_b.original_filename}' "
            f"({amount_b:.2f} {currency_b})."
        )

    # The one place a percentage tolerance is used on purpose: this compares
    # amounts across DIFFERENT documents (an invoice against the payment that
    # settled it), where bank charges and currency-conversion rounding make
    # small differences legitimate. Arithmetic within one document is checked
    # to the cent (app/services/field_validation_service.py).
    if abs(amount_a - amount_b) > max(0.01, max(amount_a, amount_b) * 0.01):
        currency_suffix_a = f" {currency_a}" if currency_a else ""
        currency_suffix_b = f" {currency_b}" if currency_b else ""
        return (
            f"Amount differs between '{doc_a.original_filename}' "
            f"({amount_a:.2f}{currency_suffix_a}) and '{doc_b.original_filename}' "
            f"({amount_b:.2f}{currency_suffix_b})."
        )
    return None


def _date_mismatch_description(doc_a: Document, doc_b: Document) -> str | None:
    raw_a, raw_b = _core_field(doc_a, "date").get("value"), _core_field(doc_b, "date").get("value")
    date_a, date_b = _parse_date(raw_a), _parse_date(raw_b)
    if date_a is None or date_b is None or date_a == date_b:
        return None
    return (
        f"Date differs between '{doc_a.original_filename}' ({raw_a!r}) and "
        f"'{doc_b.original_filename}' ({raw_b!r})."
    )


def _issuer_mismatch_description(doc_a: Document, doc_b: Document) -> str | None:
    name_a, name_b = _core_field(doc_a, "issuer").get("value"), _core_field(doc_b, "issuer").get("value")
    if not name_a or not name_b:
        return None
    # `processor=utils.default_process` lowercases, strips punctuation, and
    # collapses whitespace before scoring — see app/services/issuer_service.py's
    # match_issuer for why that matters (case/punctuation differences
    # alone shouldn't read as a different issuer).
    if fuzz.WRatio(name_a, name_b, processor=utils.default_process) >= _ISSUER_NAME_SIMILARITY_THRESHOLD:
        return None
    return f"Issuer differs between '{doc_a.original_filename}' ({name_a!r}) and '{doc_b.original_filename}' ({name_b!r})."


# field_name -> description-building function. Each returns a
# human-readable mismatch description, or None if the fields agree (or
# can't be compared).
_FIELD_CHECKS = (
    ("amount", _amount_mismatch_description),
    ("date", _date_mismatch_description),
    ("issuer", _issuer_mismatch_description),
)

_CLAIM_EVIDENCE_NOTE = (
    " Expected for a claim document compared against its own evidence document "
    "(see each document's own extracted fields above) — treat as context, not a red flag."
)


def _is_claim_evidence_pair(doc_a: Document, doc_b: Document) -> bool:
    roles = {get_document_role(doc_a.document_type), get_document_role(doc_b.document_type)}
    return roles == {"claim", "evidence"}


def _finding_severity(field_name: str, *, claim_evidence_pair: bool) -> FindingSeverity:
    if field_name == "amount":
        # The actual reconciliation signal this check exists for (does
        # the payment match the invoice total?) — always high, claim vs.
        # evidence or not.
        return FindingSeverity.high
    return FindingSeverity.low if claim_evidence_pair else FindingSeverity.medium


def find_cross_document_mismatches(documents: list[Document]) -> list[dict[str, Any]]:
    """Compare every pair of `documents` on amount/date/issuer. Returns
    one finding dict per mismatched (document pair, field) — each is
    ready to construct a CrossDocumentFinding row from directly
    (`CrossDocumentFinding(case_id=..., **finding)`)."""
    findings: list[dict[str, Any]] = []
    for doc_a, doc_b in combinations(documents, 2):
        claim_evidence_pair = _is_claim_evidence_pair(doc_a, doc_b)
        for field_name, describe_mismatch in _FIELD_CHECKS:
            description = describe_mismatch(doc_a, doc_b)
            if description is None:
                continue
            if claim_evidence_pair and field_name != "amount":
                description += _CLAIM_EVIDENCE_NOTE
            findings.append(
                {
                    "field_name": field_name,
                    # Matches the check name in SPECIFICATION.md's task description
                    # for this validation step.
                    "finding_type": "cross_document_consistency",
                    "severity": _finding_severity(field_name, claim_evidence_pair=claim_evidence_pair),
                    "description": description,
                    "document_ids": [str(doc_a.id), str(doc_b.id)],
                }
            )
    return findings
