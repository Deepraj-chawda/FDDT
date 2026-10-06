"""verify_issuer (app/services/issuer_service.py): "not checked" when the
registry has nothing that could apply, and OCR-noise normalisation."""
import pytest

from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.services.issuer_service import issuer_name_variants, verify_issuer

GIPA_NAME = "أكاديمية الخليج الدولية الخاصة THE GULF INTERNATIONAL PRIVATE ACADEMYO"


def _company(db):
    return db.info["test_company_id"]


def _fields(issuer, *, currency="AED", grade=None, items=(), total=None, words=None, date="2023-07-05", iban=None):
    additional = []
    if grade:
        additional.append({"field_name": "grade", "value": grade})
    if iban:
        additional.append({"field_name": "iban", "value": iban})
    return {
        "core_fields": {
            "issuer": {"value": issuer},
            "amount": {"value": total, "currency": currency},
            "date": {"value": date},
        },
        "additional_fields": additional,
        "line_items": [
            {"description": d, "line_total": a, "counts_toward_total": True} for d, a in items
        ],
        "amount_in_words": {"text": words},
    }


def _add(db, name, type_=IssuerType.school, country="AE", arabic=None):
    row = IssuerRegistry(name=name, name_arabic=arabic, type=type_, country=country)
    db.add(row)
    db.commit()
    return row


# --- not checked vs not in registry -----------------------------------------------------


def test_empty_registry_is_not_checked_with_reason(db_session):
    result = verify_issuer(db_session, _company(db_session), _fields("HEADSTART NURSERY"), document_type="school_document")
    assert result["result"] == "not_checked"
    assert "registry has no active entries for school issuers in AE" in result["details"]["reason"]


def test_registry_without_entries_for_this_kind_or_country_is_not_checked(db_session):
    _add(db_session, "Meridian Industrial Supplies LLC", IssuerType.vendor)
    _add(db_session, "Riyadh Modern School", country="SA")
    assert verify_issuer(
        db_session, _company(db_session), _fields("HEADSTART NURSERY"), document_type="school_document"
    )["result"] == "not_checked"


def test_unmatched_issuer_with_relevant_entries_is_flagged(db_session):
    _add(db_session, "Oakridge International Academy")
    result = verify_issuer(db_session, _company(db_session), _fields("HEADSTART NURSERY"), document_type="school_document")
    assert result["result"] == "flag" and "reason" not in result["details"]


def test_entries_with_no_country_on_file_still_count(db_session):
    _add(db_session, "Oakridge International Academy", country=None)
    assert verify_issuer(
        db_session, _company(db_session), _fields("HEADSTART NURSERY"), document_type="school_document"
    )["result"] == "flag"


def test_a_match_still_passes_whatever_the_kind(db_session):
    _add(db_session, "Headstart Nursery", IssuerType.vendor, country=None)
    assert verify_issuer(
        db_session, _company(db_session), _fields("HEADSTART NURSERY"), document_type="school_document"
    )["result"] == "pass"


# --- OCR noise -------------------------------------------------------------------------


def test_name_variants_strip_trademark_misreads_and_split_scripts():
    assert issuer_name_variants(GIPA_NAME) == [
        GIPA_NAME,
        "أكاديمية الخليج الدولية الخاصة THE GULF INTERNATIONAL PRIVATE ACADEMY",
        "أكاديمية الخليج الدولية الخاصة",
        "THE GULF INTERNATIONAL PRIVATE ACADEMY",
    ]
    assert issuer_name_variants("Acme Academy®") == ["Acme Academy®", "Acme Academy"]
    # A real word ending in O is not touched.
    assert issuer_name_variants("STUDIO NINE LLC") == ["STUDIO NINE LLC"]


@pytest.mark.parametrize("registry_name, arabic", [
    ("The Gulf International Private Academy", None),
    ("Some Other English Name", "أكاديمية الخليج الدولية الخاصة"),
])
def test_bilingual_ocr_name_matches_either_script(db_session, registry_name, arabic):
    _add(db_session, registry_name, arabic=arabic)
    result = verify_issuer(db_session, _company(db_session), _fields(GIPA_NAME), document_type="school_document")
    assert result["result"] == "pass", result
    assert result["details"]["match_score"] >= 95

