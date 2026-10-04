"""
Unit tests for app/services/cross_document_service.py — pure logic, no
DB/Celery involved. Document objects are constructed transiently (never
added to a session) since only their `id`, `original_filename`, and
`extracted_fields` attributes are read. Amount/date use the normalized
shape produced by app/services/llm_service.py (plain float + currency,
ISO 8601), matching production data.
"""
import uuid

from app.models.cross_document_finding import FindingSeverity
from app.services.cross_document_service import find_cross_document_mismatches


def _document(*, filename: str, issuer=None, date=None, amount=None, currency="USD", document_type=None):
    from app.models.document import Document

    def _str_field(value):
        return None if value is None else {"value": value, "confidence": 0.9, "uncertain": False}

    def _date_field(value):
        return None if value is None else {"value": value, "raw_text": value, "confidence": 0.9, "uncertain": False}

    def _amount_field(value):
        return (
            None
            if value is None
            else {"value": value, "currency": currency, "raw_text": str(value), "confidence": 0.9, "uncertain": False}
        )

    core_fields = {}
    if issuer is not None:
        core_fields["issuer"] = _str_field(issuer)
    if date is not None:
        core_fields["date"] = _date_field(date)
    if amount is not None:
        core_fields["amount"] = _amount_field(amount)

    return Document(
        id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        uploaded_by_user_id=uuid.uuid4(),
        original_filename=filename,
        blob_storage_path="https://fake/blob",
        file_hash="0" * 64,
        document_type=document_type,
        extracted_fields={"core_fields": core_fields, "additional_fields": []},
    )


def test_no_findings_when_everything_matches():
    docs = [
        _document(filename="invoice.pdf", issuer="Acme LLC", date="2026-08-25", amount=150.0),
        _document(filename="receipt.pdf", issuer="Acme LLC", date="2026-08-25", amount=150.0),
    ]
    assert find_cross_document_mismatches(docs) == []


def test_amount_mismatch_is_found():
    docs = [
        _document(filename="invoice.pdf", amount=150.0),
        _document(filename="receipt.pdf", amount=999.0),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["field_name"] == "amount"
    assert findings[0]["finding_type"] == "cross_document_consistency"
    assert set(findings[0]["document_ids"]) == {str(docs[0].id), str(docs[1].id)}
    # The actual reconciliation signal this check exists for — always
    # high severity, regardless of the documents' roles.
    assert findings[0]["severity"] == FindingSeverity.high


def test_amount_mismatch_is_high_severity_even_between_claim_and_evidence():
    docs = [
        _document(filename="invoice.pdf", amount=150.0, document_type="vendor_invoice"),
        _document(filename="receipt.pdf", amount=999.0, document_type="payment_evidence"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["severity"] == FindingSeverity.high


def test_amount_mismatch_within_rounding_tolerance_is_not_flagged():
    docs = [
        _document(filename="invoice.pdf", amount=150.00),
        _document(filename="receipt.pdf", amount=150.01),
    ]
    assert find_cross_document_mismatches(docs) == []


def test_amount_currency_mismatch_is_flagged_even_if_numerically_equal():
    docs = [
        _document(filename="invoice.pdf", amount=100.0, currency="USD"),
        _document(filename="receipt.pdf", amount=100.0, currency="AED"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["field_name"] == "amount"
    assert "currency" in findings[0]["description"].lower()


def test_date_mismatch_is_found():
    docs = [
        _document(filename="invoice.pdf", date="2026-08-25"),
        _document(filename="receipt.pdf", date="2026-08-26"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["field_name"] == "date"
    # Neither document has a document_type set (role "pending") — not a
    # recognized claim/evidence pair, so this stays at the default
    # same-role severity.
    assert findings[0]["severity"] == FindingSeverity.medium


def test_date_mismatch_between_claim_and_evidence_is_downgraded_to_low():
    docs = [
        _document(filename="invoice.pdf", date="2026-08-22", document_type="vendor_invoice"),
        _document(filename="receipt.pdf", date="2026-08-24", document_type="payment_evidence"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["severity"] == FindingSeverity.low
    assert "claim" in findings[0]["description"].lower()


def test_date_mismatch_between_two_claim_documents_stays_medium():
    docs = [
        _document(filename="invoice_a.pdf", date="2026-08-22", document_type="vendor_invoice"),
        _document(filename="invoice_b.pdf", date="2026-08-24", document_type="commercial_invoice"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["severity"] == FindingSeverity.medium


def test_issuer_mismatch_between_claim_and_evidence_is_downgraded_to_low():
    docs = [
        _document(filename="invoice.pdf", issuer="Acme LLC", document_type="vendor_invoice"),
        _document(
            filename="receipt.pdf",
            issuer="Some Payment Processor",
            document_type="payment_evidence",
        ),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["severity"] == FindingSeverity.low


def test_date_match_is_not_flagged():
    docs = [
        _document(filename="invoice.pdf", date="2026-08-25"),
        _document(filename="receipt.pdf", date="2026-08-25"),
    ]
    assert find_cross_document_mismatches(docs) == []


def test_issuer_name_minor_variation_is_not_flagged():
    docs = [
        _document(filename="invoice.pdf", issuer="Al Nukhba Technical Systems Est."),
        _document(filename="receipt.pdf", issuer="Al Nukhba Technical Systems Est"),
    ]
    assert find_cross_document_mismatches(docs) == []


def test_issuer_name_genuinely_different_is_flagged():
    docs = [
        _document(filename="invoice.pdf", issuer="Acme LLC"),
        _document(filename="receipt.pdf", issuer="Totally Different Trading Co"),
    ]
    findings = find_cross_document_mismatches(docs)
    assert len(findings) == 1
    assert findings[0]["field_name"] == "issuer"


def test_missing_field_on_either_side_is_not_compared():
    docs = [
        _document(filename="invoice.pdf", amount=150.0),
        _document(filename="receipt.pdf", amount=None),
    ]
    assert find_cross_document_mismatches(docs) == []


def test_single_document_has_no_pairs_to_compare():
    docs = [_document(filename="invoice.pdf", amount=150.0)]
    assert find_cross_document_mismatches(docs) == []


def test_three_documents_compares_every_pair():
    docs = [
        _document(filename="a.pdf", amount=100.0),
        _document(filename="b.pdf", amount=100.0),
        _document(filename="c.pdf", amount=999.0),
    ]
    findings = find_cross_document_mismatches(docs)
    # a-c and b-c mismatch; a-b matches.
    assert len(findings) == 2
