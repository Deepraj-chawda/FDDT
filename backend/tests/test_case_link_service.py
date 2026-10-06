"""Cross-case links (app/services/case_link_service.py): a reviewer note
that is never scored."""
from app.models.document_check import DocumentCheckType
from app.services.case_link_service import _names_match, _name_tokens, find_linked_cases
from app.services.risk_rule_seed import SEED_RULES
from tests.helpers_risk import add_document, make_case

MFP_INFO = {"/Title": "00206BE9FB63230707154842", "/Creator": "PPS - SFO2-A-MFP01", "/Producer": "Develop ineo+ 759"}


def _metadata(info):
    return {"result": "pass", "details": [{"finding": "info_dictionary", "severity": "info", "data": info}]}


def _doc(db, user, info=None, fields=None):
    case = make_case(db, user)
    checks = {DocumentCheckType.metadata_forensics: _metadata(info or {})}
    doc = add_document(db, case, user, "doc.pdf", checks=checks)
    doc.extracted_fields = {"additional_fields": [{"field_name": k, "value": v} for k, v in (fields or {}).items()]}
    db.commit()
    return case


def _links(db, case):
    return {link.case_number: link.reasons for link in find_linked_cases(db, case.company_id, case.id)}


def test_reference_cases_link_by_device_and_parent(db_session, seeded_user):
    gipa = _doc(db_session, seeded_user, MFP_INFO, {
        "student_name": "GHAYA AHMED SALEM ABDULLA ALKINDI",
        "parent_name": "Mr. Ahmed Salem Abdulla AlKindi",
        "family_number": "1245",
    })
    headstart = _doc(db_session, seeded_user, dict(MFP_INFO, **{"/Title": "00206BE9FB63220906122019"}), {
        "student_name": "HAMDA AHMED SALEM ALKINDI",
        "student_id": "HSN1278",
    })
    reasons = _links(db_session, gipa)[headstart.case_number]
    assert reasons[0] == "same scanning device (PPS - SFO2-A-MFP01, serial 00206BE9FB63)"
    assert reasons[1].startswith("same parent/guardian ('Mr. Ahmed Salem Abdulla AlKindi'")
    assert gipa.case_number in _links(db_session, headstart)


def test_plain_titles_and_unrelated_names_do_not_link(db_session, seeded_user):
    a = _doc(db_session, seeded_user, {"/Title": "Invoice", "/Creator": "Microsoft Word"}, {"parent_name": "Ali Hassan"})
    _doc(db_session, seeded_user, {"/Title": "Invoice", "/Creator": "Microsoft Word"}, {"parent_name": "Ali Hassan"})
    _doc(db_session, seeded_user, MFP_INFO, {"parent_name": "Omar Salem Ahmed AlKindi"})
    assert _links(db_session, a) == {}


def test_same_ids_link_only_within_the_same_kind(db_session, seeded_user):
    a = _doc(db_session, seeded_user, fields={"family_number": "1245"})
    b = _doc(db_session, seeded_user, fields={"family_number": "1245"})
    _doc(db_session, seeded_user, fields={"student_id": "1245"})
    assert _links(db_session, a) == {b.case_number: ["same family number (1245)"]}


def test_name_matching_allows_a_left_out_middle_name_only():
    assert _names_match(_name_tokens("AHMED SALEM ALKINDI"), _name_tokens("Mr. Ahmed Salem Abdulla AlKindi"))
    assert not _names_match(_name_tokens("AHMED SALEM ALKINDI"), _name_tokens("Ahmed Khalid AlKindi"))
    assert not _names_match(_name_tokens("Ali Hassan"), _name_tokens("Ali Hassan"))  # too short to mean anything


def test_links_are_never_scored():
    assert not any("link" in rule["rule_id"] for rule in SEED_RULES)
