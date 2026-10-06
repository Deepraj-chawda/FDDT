"""verify_issuer (app/services/issuer_service.py): "not checked" when the
registry has nothing that could apply, OCR-noise normalisation, and the fee
benchmark sub-check (app/services/fee_benchmark_service.py)."""
import json
from pathlib import Path

import pytest

from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.services.fee_benchmark_service import normalize_grade
from app.services.issuer_service import issuer_name_variants, verify_issuer
from scripts.seed_reference_issuers import DATA, seed

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


# --- fee benchmarks ---------------------------------------------------------------------


@pytest.fixture()
def seeded(db_session):
    seed(db_session, _company(db_session), json.loads(Path(DATA).read_text(encoding="utf-8")))
    db_session.commit()


GIPA_ITEMS = [("Tuition Fees", 52130.0), ("Books", 650.0), ("Uniform", 300.0), ("Technology Fee", 550.0),
              ("Transport", 3510.0)]


def test_gipa_tuition_far_above_published_fee_is_flagged(db_session, seeded):
    fields = _fields(GIPA_NAME, grade="KG2", items=GIPA_ITEMS, total=57182.5)
    result = verify_issuer(db_session, _company(db_session), fields, document_type="school_document",
                           ocr_text="registered in our school for the academic year 2023-2024.")
    assert result["result"] == "pass"  # the issuer itself is known
    bench = result["details"]["fee_benchmark"]
    assert bench["status"] == "flag", bench
    assert "Tuition Fees 52,130.00 is 306% above the published 12,840.00" in bench["reason"]
    assert "not verified" in bench["reason"]
    # One inflated item among exact ones.
    assert "Books 650.00, Uniform 300.00" in bench["reason"] and "altered figure" in bench["reason"]
    # Transport only has a 2020-21 figure: more than a year away, not compared.
    assert "transport" not in {c["key"] for c in bench["comparisons"]}


def test_year_adjustment_within_one_year(db_session, seeded):
    # A 2024-25 invoice: 2023-24 benchmark + 5%.
    fields = _fields(GIPA_NAME, grade="KG2", items=[("Tuition Fees", 13482.0)], total=13482.0, date="2024-09-01")
    bench = verify_issuer(db_session, _company(db_session), fields, document_type="school_document")["details"]["fee_benchmark"]
    assert bench["status"] == "pass"
    assert bench["comparisons"][0]["benchmark"] == pytest.approx(13482.0)


def test_headstart_total_far_above_and_words_fit(db_session, seeded):
    items = [("FEE FOR TERM 1", 22000.0), ("FEE FOR TERM 2", 16500.0), ("FEE FOR TERM 3", 16600.0)]
    fields = _fields("HEADSTART NURSERY", items=items, total=55500.0,
                     words="TWENTY THOUSAND FIVE HUNDRED AED ONLY", date="2022-09-06")
    result = verify_issuer(db_session, _company(db_session), fields, document_type="school_document")
    bench = result["details"]["fee_benchmark"]
    assert bench["status"] == "flag", bench
    assert "Total 55,500.00 is 178% above the published 20,000.00" in bench["reason"]
    assert "amount in words (20,500.00) is 2% above" in bench["reason"]
    assert "words fit the published fee while the figures do not" in bench["reason"]


def test_benchmark_not_checked_without_registry_match_or_rows(db_session, seeded):
    unknown = verify_issuer(db_session, _company(db_session), _fields("Unknown School"), document_type="school_document")
    assert unknown["details"]["fee_benchmark"]["status"] == "not_checked"
    other_grade = verify_issuer(
        db_session, _company(db_session), _fields(GIPA_NAME, grade="Grade 5", items=GIPA_ITEMS, total=1.0),
        document_type="school_document",
    )
    assert other_grade["details"]["fee_benchmark"]["status"] == "not_checked"
    assert "(GRADE 5)" in other_grade["details"]["fee_benchmark"]["reason"]


def test_seed_is_idempotent(db_session):
    data = json.loads(Path(DATA).read_text(encoding="utf-8"))
    assert seed(db_session, _company(db_session), data) == (2, 8)
    assert seed(db_session, _company(db_session), data) == (0, 0)


@pytest.mark.parametrize("raw, grade", [("KG 2", "KG2"), ("kg-2", "KG2"), ("Grade 5", "GRADE 5"), ("Year 3", "YEAR 3")])
def test_normalize_grade(raw, grade):
    assert normalize_grade(raw) == grade
