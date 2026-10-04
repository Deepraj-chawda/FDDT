"""Field-level exception regions: the boxes behind the purple highlights.

Covers app/services/field_regions.py, field_exception_service.py, the regions
embedded by field_validation_service, and what GET /cases/{id} hands the live
Case Detail UI (which draws them as overlays — nothing here is burned into a file).
"""
import hashlib
import uuid
from types import SimpleNamespace

from app.models.cross_document_finding import CrossDocumentFinding, FindingSeverity
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.field_exception_service import cross_document_regions, with_field_regions
from app.services.field_regions import format_field_value, make_region, valid_box
from app.services.field_validation_service import validate_fields
from tests.helpers_risk import make_case


def _box(y=0.4, page=1):
    return {"page": page, "x": 0.2, "y": y, "width": 0.2, "height": 0.03}


def _field(value, box=None, currency="USD"):
    field = {"value": value, "currency": currency, "confidence": 0.9, "uncertain": False}
    if box:
        field["bounding_box"] = box
    return field


def test_valid_box_clamps_and_rejects_degenerate_boxes():
    assert valid_box({"page": 1, "x": 0.9, "y": 0.5, "width": 0.5, "height": 0.1})["width"] == 0.1  # clamped to the page
    for bad in (None, {}, {"page": 0, "x": 0, "y": 0, "width": 0.1, "height": 0.1},
                {"page": 1, "x": 0, "y": 0, "width": 0, "height": 0.1}, {"page": "x", "x": 0, "y": 0, "width": 1, "height": 1}):
        assert valid_box(bad) is None


def test_format_field_value():
    assert format_field_value("amount", {"value": 9030.0, "currency": "USD"}) == "9,030.00 USD"
    assert format_field_value("tax_rate", {"value": 5.0}) == "5%"
    assert format_field_value("issuer", {"value": "Acme"}) == "Acme"
    assert format_field_value("date", {"value": None}) == "not found"


def test_make_region_needs_a_stored_location():
    assert make_region({"amount": _field(1.0)}, "amount", "cap") is None
    region = make_region({"amount": _field(1.0, _box())}, "amount", "cap")
    assert region["label"] == "Total amount" and region["caption"] == "cap" and region["bounding_box"]["page"] == 1


def test_validation_embeds_regions_only_on_flagged_subchecks_with_located_fields():
    located = {"core_fields": {"amount": _field(4520.0, _box(0.6)), "subtotal": _field(4000.0, _box(0.5))}, "additional_fields": []}
    sub = validate_fields(located)["details"]["total_tax_consistency"]
    assert sub["status"] == "flag"
    assert {r["field"] for r in sub["regions"]} == {"amount", "subtotal"}
    amount = next(r for r in sub["regions"] if r["field"] == "amount")
    assert amount["caption"] == "Field exception: Total amount (4,520.00 USD vs expected 4,000.00)"

    unlocated = {"core_fields": {"amount": _field(4520.0), "subtotal": _field(4000.0)}, "additional_fields": []}
    assert "regions" not in validate_fields(unlocated)["details"]["total_tax_consistency"]  # still flagged, just not drawable

    ok = {"core_fields": {"amount": _field(4000.0, _box()), "subtotal": _field(4000.0, _box(0.5))}, "additional_fields": []}
    assert "regions" not in validate_fields(ok)["details"]["total_tax_consistency"]  # passes -> nothing to highlight


def test_with_field_regions_fills_legacy_rows_without_mutating_them():
    extracted = {"core_fields": {"amount": _field(4520.0, _box(0.6)), "subtotal": _field(4000.0, _box(0.5))}, "additional_fields": []}
    legacy = {"result": "flag", "details": {"total_tax_consistency": {"status": "flag", "reason": "Total 4520.00 ..."}}}
    enriched = with_field_regions(legacy, extracted)
    assert len(enriched["details"]["total_tax_consistency"]["regions"]) == 2
    assert "regions" not in legacy["details"]["total_tax_consistency"]  # the stored row is untouched
    assert with_field_regions(legacy, None) is legacy
    assert with_field_regions({"result": "pass", "details": []}, extracted) == {"result": "pass", "details": []}


def _doc(name, amount, box):
    return SimpleNamespace(id=uuid.uuid4(), original_filename=name,
                           extracted_fields={"core_fields": {"amount": _field(amount, box)}})


def test_cross_document_regions_cover_both_documents_with_the_others_value():
    a, b = _doc("invoice.pdf", 9030.0, _box(0.30)), _doc("payment.pdf", 7250.0, _box(0.55))
    by_id = {str(a.id): a, str(b.id): b}
    regions = cross_document_regions("amount", [str(a.id), str(b.id)], by_id)

    assert [r["document_id"] for r in regions] == [str(a.id), str(b.id)]
    assert regions[0]["caption"] == "Field mismatch: Total amount (9,030.00 USD vs 7,250.00 USD)"
    assert regions[1]["caption"] == "Field mismatch: Total amount (7,250.00 USD vs 9,030.00 USD)"
    assert regions[0]["other"] == [{"document_id": str(b.id), "document_filename": "payment.pdf", "value": "7,250.00 USD"}]
    assert regions[0]["bounding_box"]["y"] == 0.3 and regions[1]["bounding_box"]["y"] == 0.55


def test_cross_document_regions_skip_the_side_with_no_stored_location():
    a, b = _doc("invoice.pdf", 9030.0, _box()), _doc("payment.pdf", 7250.0, None)
    regions = cross_document_regions("amount", [str(a.id), str(b.id)], {str(a.id): a, str(b.id): b})
    assert [r["document_id"] for r in regions] == [str(a.id)]
    assert cross_document_regions("amount", None, {}) == []


# ---- what the live UI receives ------------------------------------------------

def _stored_doc(db, fake_storage, case, user, name, fields):
    path = f"{case.id}/{uuid.uuid4()}_{name}"
    content = name.encode()
    fake_storage.uploads[path] = content
    doc = Document(
        case_id=case.id, uploaded_by_user_id=user.id, original_filename=name,
        blob_storage_path=f"https://fake.blob.core.windows.net/documents/{path}",
        file_hash=hashlib.sha256(content).hexdigest(), content_type="application/pdf",
        document_type="vendor_invoice", processing_status=DocumentProcessingStatus.complete, extracted_fields=fields,
    )
    db.add(doc)
    db.commit()
    return doc


def test_case_detail_gives_the_live_ui_regions_for_both_documents_and_enriched_checks(
    client, db_session, fake_storage, seeded_user, auth_headers
):
    case = make_case(db_session, seeded_user)
    a = _stored_doc(db_session, fake_storage, case, seeded_user, "invoice.pdf", {
        "core_fields": {"amount": _field(4520.0, _box(0.6)), "subtotal": _field(4000.0, _box(0.5))}, "additional_fields": []})
    b = _stored_doc(db_session, fake_storage, case, seeded_user, "payment.pdf", {
        "core_fields": {"amount": _field(4250.0, _box(0.3))}, "additional_fields": []})
    db_session.add(CrossDocumentFinding(
        case_id=case.id, field_name="amount", finding_type="cross_document_consistency", severity=FindingSeverity.high,
        description="Amount differs.", document_ids=[str(a.id), str(b.id)]))
    # a field-validation row stored the legacy way (flagged, no regions)
    db_session.add(DocumentCheck(
        document_id=a.id, check_type=DocumentCheckType.field_validation, status=DocumentCheckStatus.completed,
        result={"result": "flag", "details": {"total_tax_consistency": {"status": "flag", "reason": "Total 4520.00 ..."}}}))
    db_session.commit()

    body = client.get(f"/cases/{case.id}", headers=auth_headers).json()

    finding = body["cross_document_findings"][0]
    assert {r["document_id"] for r in finding["regions"]} == {str(a.id), str(b.id)}
    on_invoice = next(r for r in finding["regions"] if r["document_id"] == str(a.id))
    assert on_invoice["caption"] == "Field mismatch: Total amount (4,520.00 USD vs 4,250.00 USD)"
    assert on_invoice["bounding_box"]["y"] == 0.6

    invoice = next(d for d in body["documents"] if d["id"] == str(a.id))
    sub = invoice["checks"][0]["result"]["details"]["total_tax_consistency"]
    assert {r["field"] for r in sub["regions"]} == {"amount", "subtotal"}
