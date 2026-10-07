"""
Tests for the risk-scoring engine (app/services/risk_scoring_service.py) —
rule matching, scoring/tiers, pipeline gating, and the versioning guarantee
that tuning a rule never changes an already-scored case.
"""
import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.case import CaseStatus, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.cross_document_finding import CrossDocumentFinding, FindingSeverity
from app.models.document_check import DocumentCheckType as T
from app.models.risk_rule import RiskRule
from app.models.risk_setting import RiskSetting
from app.models.signature_match import ComparisonScope, SignatureMatch, SignatureMatchResult
from app.models.signature_reference import SignatureReference
from app.models.user import User, UserRole
from app.services.risk_rule_seed import SEED_RULES, seed_risk_rules
from app.services.risk_scoring_service import (
    latest_assessment,
    load_current_rules,
    pipeline_status,
    render_reason,
    score_case,
    tier_for_score,
)

from tests.helpers_risk import PASS, add_cross_doc_event, add_document, finding, make_case


@pytest.fixture()
def user(db_session):
    u = User(email="submitter@x.com", hashed_password="x", role=UserRole.user, is_active=True)
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def rules(db_session):
    seed_risk_rules(db_session, db_session.info["test_company_id"])
    db_session.commit()


def _flag(details, severity_result="flag"):
    return {"result": severity_result, "details": details}


def _rule(db, rule_id) -> RiskRule:
    return db.execute(select(RiskRule).where(RiskRule.rule_id == rule_id).order_by(RiskRule.version.desc())).scalars().first()


def _new_version(db, rule_id, **changes) -> RiskRule:
    """What the admin API does: INSERT version+1, leave the old row alone."""
    cur = _rule(db, rule_id)
    row = RiskRule(
        rule_id=cur.rule_id, category=cur.category, check_type=cur.check_type, condition=cur.condition,
        weight=changes.get("weight", cur.weight), severity=cur.severity, reason_template=cur.reason_template,
        is_active=changes.get("is_active", cur.is_active), version=cur.version + 1,
        effective_from=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------------- the seed

def test_seed_covers_every_check_family_with_unique_ids(db_session):
    assert seed_risk_rules(db_session, db_session.info["test_company_id"]) == len(SEED_RULES)
    ids = [r["rule_id"] for r in SEED_RULES]
    assert len(ids) == len(set(ids))
    assert all(len(i) <= 64 for i in ids)
    assert {r["category"] for r in SEED_RULES} == {"forensics", "consistency", "verification", "duplication"}
    # Sub-conditions of one check are separate rules (ELA region vs anti-forensic).
    assert {"ela.tamper_region_detected", "ela.anti_forensic_signal"} <= set(ids)
    assert {"duplicate.cross_case_match", "cross_doc.amount_mismatch", "issuer.not_in_registry"} <= set(ids)
    assert seed_risk_rules(db_session, db_session.info["test_company_id"]) == 0  # idempotent


def test_seed_matches_what_the_checks_actually_emit():
    """Every finding name a rule looks for must be one the check code can
    produce — guards against a rule that can never fire."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    source = "".join(p.read_text(encoding="utf-8") for p in root.rglob("*.py") if p.name != "risk_rule_seed.py")
    for spec in SEED_RULES:
        name = spec["condition"].get("finding")
        if name and not name.startswith("visual_"):
            assert f'"{name}"' in source, f"{spec['rule_id']} matches unknown finding {name!r}"
    for sub in [r["condition"]["sub_check"] for r in SEED_RULES if r["condition"]["match"] == "sub_check"]:
        assert f'"{sub}"' in source


# ----------------------------------------------------------- matching/scoring

def test_fired_rule_produces_reason_weight_and_document(db_session, user, rules):
    case = make_case(db_session, user)
    doc = add_document(db_session, case, user, "invoice.pdf", checks={
        T.metadata_forensics: _flag([finding("editing_software_detected", "high", "Producer mentions Photoshop.")]),
    })
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a is not None
    assert a.raw_score == 30 and a.score == 30
    assert a.tier == RiskTier.medium  # 30 is the medium cutoff
    (reason,) = a.triggered_reasons
    assert reason["rule_id"] == "metadata.editing_software_detected"
    assert reason["rule_version"] == 1
    assert reason["document_filename"] == "invoice.pdf"
    assert reason["document_id"] == str(doc.id)
    assert "invoice.pdf" in reason["reason"] and "Photoshop" in reason["reason"]
    assert reason["weight"] == 30 and reason["severity"] == "high"


def test_font_mismatch_scores_text_layer_and_scan_findings_separately(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "digital.pdf", checks={
        T.font_consistency: _flag([
            finding("font_family_inventory", "info", "1 of 1 page(s) analysed; 2 font families."),
            finding("font_inconsistency", "high", "Page 1: '1,450.00' is set in Times-Roman."),
            finding("font_inconsistency", "high", "Page 1: '14,500.00' is set in Times-Roman."),
        ]),
    })
    add_document(db_session, case, user, "scan.pdf", checks={
        T.font_consistency: _flag([finding("font_inconsistency", "medium", "Page 1 (scanned): '22,000' looks different.")]),
    })
    add_cross_doc_event(db_session, case)  # a two-document case waits for its cross-document check
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    by_rule = {r["rule_id"]: r for r in a.triggered_reasons}
    assert by_rule.keys() == {"font.inconsistency", "font.inconsistency_scanned"}
    digital, scan = by_rule["font.inconsistency"], by_rule["font.inconsistency_scanned"]
    assert (digital["weight"], digital["severity"], digital["document_filename"]) == (40, "high", "digital.pdf")
    assert "2 piece(s) of text" in digital["reason"] and "1,450.00" in digital["reason"]
    assert (scan["weight"], scan["severity"], scan["document_filename"]) == (25, "medium", "scan.pdf")
    assert "an estimate" in scan["reason"] and "22,000" in scan["reason"]


def test_clean_case_scores_zero_low(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf")
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert (a.score, a.tier, a.triggered_reasons) == (0, RiskTier.low, [])


def test_rule_fires_once_per_document_with_count_in_reason(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "scan.pdf", checks={
        T.error_level_analysis: _flag([
            finding("recompression_error_region", "medium", page=2),
            finding("recompression_error_region", "medium", page=3),
            finding("recompression_error_region", "medium", page=3),
        ]),
    })
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.raw_score == 25  # once, not 3 x 25
    assert "3 localized" in a.triggered_reasons[0]["reason"]
    assert "page 2" in a.triggered_reasons[0]["reason"]


def test_same_rule_on_two_documents_fires_for_each(db_session, user, rules):
    case = make_case(db_session, user)
    bad = {T.copy_move_detection: _flag([finding("copy_move_cluster", "high")])}
    add_document(db_session, case, user, "a.pdf", checks=bad)
    add_document(db_session, case, user, "b.pdf", checks=bad)
    add_cross_doc_event(db_session, case)
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.raw_score == 70
    assert {r["document_filename"] for r in a.triggered_reasons} == {"a.pdf", "b.pdf"}


def test_score_is_capped_at_100_and_tiers_by_threshold(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={
        T.metadata_forensics: _flag([finding("editing_software_detected"), finding("metadata_entirely_stripped")]),
        T.copy_move_detection: _flag([finding("copy_move_cluster")]),
        T.duplicate_detection: _flag([finding("near_duplicate_page")]),
        T.error_level_analysis: _flag([finding("anti_forensic_signal")]),
    })
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    # The two metadata rules (30 + 12) count at most the metadata cap, 40.
    assert a.raw_score == 40 + 35 + 40 + 30
    assert a.risk_rules_version_snapshot["group_caps"] == {"metadata": {"cap": 40, "points": 42}}
    assert a.score == 100 and a.tier == RiskTier.high
    assert case.risk_tier == RiskTier.high


def test_metadata_cap_is_the_companys_setting(db_session, user, rules):
    row = db_session.execute(select(RiskSetting)).scalars().one()
    row.metadata_score_cap = 100  # no cap
    db_session.commit()
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={
        T.metadata_forensics: _flag([finding("editing_software_detected"), finding("metadata_entirely_stripped")]),
    })
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.raw_score == 42 and a.risk_rules_version_snapshot["group_caps"] == {}
    assert a.risk_rules_version_snapshot["metadata_score_cap"] == 100


@pytest.mark.parametrize("score,tier", [(0, "low"), (29, "low"), (30, "medium"), (59, "medium"), (60, "high"), (100, "high")])
def test_tier_boundaries(score, tier):
    assert tier_for_score(score, 30, 60).value == tier


def test_custom_thresholds_are_used_and_snapshotted(db_session, user, rules):
    row = db_session.execute(select(RiskSetting)).scalars().one()
    row.medium_threshold, row.high_threshold = 10, 25
    db_session.commit()
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected")])})
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.tier == RiskTier.high  # 30 >= 25
    assert a.risk_rules_version_snapshot["thresholds"] == {"medium": 10, "high": 25}


def test_inactive_rule_does_not_fire_and_does_not_warn(db_session, user, rules, caplog):
    _new_version(db_session, "metadata.editing_software_detected", is_active=False)
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected")])})
    with caplog.at_level(logging.WARNING):
        a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.score == 0
    assert "No risk rule covers" not in caplog.text


def test_finding_with_no_rule_is_skipped_with_a_warning_not_a_crash(db_session, user, rules, caplog):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("brand_new_finding", "high")])})
    with caplog.at_level(logging.WARNING):
        a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a.score == 0
    assert "brand_new_finding" in caplog.text


def test_low_severity_and_info_findings_are_not_scored(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={
        T.metadata_forensics: _flag([finding("document_id_chain", "info"), finding("optional_content_groups", "low")]),
    })
    assert score_case(db_session, db_session.info["test_company_id"], case.id).score == 0


def test_incremental_update_single_vs_multiple_are_separate_rules(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "one.pdf", checks={T.metadata_forensics: _flag([finding("incremental_updates_present", "medium")])})
    add_document(db_session, case, user, "many.pdf", checks={T.metadata_forensics: _flag([finding("incremental_updates_present", "high")])})
    add_cross_doc_event(db_session, case)
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    by_doc = {r["document_filename"]: r["rule_id"] for r in a.triggered_reasons}
    assert by_doc == {"one.pdf": "metadata.incremental_update_single", "many.pdf": "metadata.incremental_update_multiple"}


def test_field_validation_issuer_and_signature_detection_rules(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "inv.pdf", checks={
        T.field_validation: _flag({
            "date_in_future": {"status": "flag", "reason": "Document date 2099-01-01 is in the future."},
            "total_tax_consistency": {"status": "pass", "reason": "ok"},
        }),
        T.issuer_verification: _flag({"issuer_name": "Acme Ltd", "match_score": 40}),
        T.signature_stamp_detection: _flag({"signature_expected": True, "detected": [], "bounding_box": None}),
    })
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    ids = {r["rule_id"] for r in a.triggered_reasons}
    assert ids == {"field.date_in_future", "issuer.not_in_registry", "signature.expected_missing"}
    reasons = {r["rule_id"]: r["reason"] for r in a.triggered_reasons}
    assert "2099-01-01" in reasons["field.date_in_future"]
    assert "Acme Ltd" in reasons["issuer.not_in_registry"]


def test_cross_document_findings_score_by_field_and_ignore_expected_noise(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "invoice.pdf")
    add_document(db_session, case, user, "receipt.pdf")
    add_cross_doc_event(db_session, case)
    for field_name, sev, desc in [
        ("amount", FindingSeverity.high, "Amount differs: 100 vs 90."),
        ("date", FindingSeverity.low, "Date differs (expected claim/evidence noise)."),
    ]:
        db_session.add(CrossDocumentFinding(case_id=case.id, field_name=field_name, finding_type="cross_document_consistency", severity=sev, description=desc))
    db_session.commit()
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert [r["rule_id"] for r in a.triggered_reasons] == ["cross_doc.amount_mismatch"]
    assert a.triggered_reasons[0]["document_id"] is None  # case-level
    assert "Amount differs: 100 vs 90." in a.triggered_reasons[0]["reason"]


def test_inconsistent_signature_comparison_fires_with_person_name(db_session, user, rules):
    case = make_case(db_session, user)
    src = add_document(db_session, case, user, "invoice.pdf")
    tgt = add_document(db_session, case, user, "evidence.pdf")
    add_cross_doc_event(db_session, case)
    ref = SignatureReference(person_name="Marcus Whitfield", source_document_id=src.id, source_case_id=case.id, created_by=user.id, is_library=True)
    db_session.add(ref)
    db_session.flush()
    db_session.add(SignatureMatch(document_id=tgt.id, case_id=case.id, signature_reference_id=ref.id,
                                  comparison_scope=ComparisonScope.in_case, result=SignatureMatchResult.inconsistent,
                                  reasoning="Different stroke patterns."))
    db_session.commit()
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    (r,) = a.triggered_reasons
    assert r["rule_id"] == "signature.inconsistent_with_reference"
    assert r["document_filename"] == "evidence.pdf"
    assert "Marcus Whitfield" in r["reason"] and "Different stroke patterns." in r["reason"]
    assert "verified" not in r["reason"].lower()  # advisory wording only


def test_a_rerun_check_replaces_the_row_and_scoring_uses_it(db_session, user, rules):
    """One row per (document, check_type): a re-run (e.g. a Celery redelivery)
    overwrites it in place, so there is never an older row to ignore."""
    from sqlalchemy import func, select

    from app.models.document_check import DocumentCheck, DocumentCheckStatus
    from app.services.check_store import save_check

    case = make_case(db_session, user)
    doc = add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected")])})
    cid = db_session.info["test_company_id"]
    assert score_case(db_session, cid, case.id).score > 0
    save_check(db_session, document=doc, check_type=T.metadata_forensics, status=DocumentCheckStatus.completed, result=PASS)
    db_session.commit()
    rows = db_session.execute(
        select(func.count()).select_from(DocumentCheck).where(
            DocumentCheck.document_id == doc.id, DocumentCheck.check_type == T.metadata_forensics
        )
    ).scalar_one()
    assert rows == 1
    assert score_case(db_session, cid, case.id).score == 0


def test_a_second_row_for_the_same_check_is_impossible(db_session, user, rules):
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from app.models.document_check import DocumentCheck, DocumentCheckStatus

    case = make_case(db_session, user)
    doc = add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: PASS})
    db_session.add(DocumentCheck(document_id=doc.id, check_type=T.metadata_forensics,
                                 status=DocumentCheckStatus.completed, result=PASS))
    with _pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_reason_template_never_raises_on_missing_placeholders():
    assert render_reason("'{document}' has {nope} issues", {"document": "a.pdf"}) == "'a.pdf' has  issues"
    assert render_reason("bad {", {}) == "bad {"


# ---------------------------------------------------------------- the pipeline gate

def test_unfinished_pipeline_is_not_scored(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", complete=False)
    st = pipeline_status(db_session, db_session.info["test_company_id"], case.id)
    assert not st.complete and "Text extraction" in st.pending[0]
    assert score_case(db_session, db_session.info["test_company_id"], case.id) is None
    assert latest_assessment(db_session, db_session.info["test_company_id"], case.id) is None
    assert case.status == CaseStatus.submitted


def test_missing_forensic_check_holds_scoring(db_session, user, rules):
    from app.models.document_check import DocumentCheck

    case = make_case(db_session, user)
    doc = add_document(db_session, case, user, "a.pdf")
    db_session.query(DocumentCheck).filter_by(document_id=doc.id, check_type=T.copy_move_detection).delete()
    db_session.commit()
    st = pipeline_status(db_session, db_session.info["test_company_id"], case.id)
    assert not st.complete and any("Copy move detection" in p for p in st.pending)


def test_multi_document_case_waits_for_the_cross_document_check(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf")
    add_document(db_session, case, user, "b.pdf")
    assert "Cross-document consistency check" in pipeline_status(db_session, db_session.info["test_company_id"], case.id).pending
    assert score_case(db_session, db_session.info["test_company_id"], case.id) is None
    add_cross_doc_event(db_session, case)
    assert pipeline_status(db_session, db_session.info["test_company_id"], case.id).complete
    assert score_case(db_session, db_session.info["test_company_id"], case.id) is not None


def test_non_pdf_documents_dont_need_forensic_checks(db_session, user, rules):
    from app.models.document import Document, DocumentProcessingStatus
    from app.models.document_check import DocumentCheck, DocumentCheckStatus

    case = make_case(db_session, user)
    doc = Document(case_id=case.id, uploaded_by_user_id=user.id, original_filename="scan.jpg", blob_storage_path="x",
                   file_hash="h" * 64, content_type="image/jpeg", processing_status=DocumentProcessingStatus.complete)
    db_session.add(doc)
    db_session.flush()
    for t in (T.field_validation, T.issuer_verification):
        db_session.add(DocumentCheck(document_id=doc.id, check_type=t, status=DocumentCheckStatus.completed, result=PASS))
    db_session.commit()
    assert pipeline_status(db_session, db_session.info["test_company_id"], case.id).complete


def test_first_score_hands_case_to_review_queue_and_audits(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf")
    score_case(db_session, db_session.info["test_company_id"], case.id)
    db_session.commit()
    assert case.status == CaseStatus.pending_manual_review
    events = {e.event_type for e in db_session.query(AuditLog).filter(AuditLog.case_id == case.id)}
    assert {"risk_assessment_completed", "case_status_changed"} <= events


def test_adding_a_document_to_a_scored_case_rescores_it(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf")
    first = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert first.score == 0
    add_document(db_session, case, user, "b.pdf", checks={T.copy_move_detection: _flag([finding("copy_move_cluster")])})
    # b's cross-doc check hasn't run yet -> held (None), and the earlier assessment stays the latest.
    assert score_case(db_session, db_session.info["test_company_id"], case.id) is None
    assert latest_assessment(db_session, db_session.info["test_company_id"], case.id).id == first.id
    add_cross_doc_event(db_session, case)
    second = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert second.id != first.id and second.score == 35
    assert db_session.query(CaseRiskAssessment).filter_by(case_id=case.id).count() == 2


# -------------------------------------------------------------------- versioning

def test_unchanged_evidence_does_not_create_another_assessment(db_session, user, rules):
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected")])})
    a1 = score_case(db_session, db_session.info["test_company_id"], case.id)
    a2 = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert a1.id == a2.id
    assert db_session.query(CaseRiskAssessment).filter_by(case_id=case.id).count() == 1


def test_tuning_a_rule_never_changes_an_already_scored_case(db_session, user, rules):
    """The firm versioning requirement."""
    case = make_case(db_session, user)
    add_document(db_session, case, user, "a.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected", description="Photoshop")])})
    a = score_case(db_session, db_session.info["test_company_id"], case.id)
    db_session.commit()
    original = (a.score, a.tier, [dict(r) for r in a.triggered_reasons], dict(a.risk_rules_version_snapshot))
    assert original[0] == 30

    v1 = _rule(db_session, "metadata.editing_software_detected")
    v2 = _new_version(db_session, "metadata.editing_software_detected", weight=5)
    assert v2.version == 2 and v2.id != v1.id

    # A stray re-trigger over the same evidence must not re-score under new weights.
    assert score_case(db_session, db_session.info["test_company_id"], case.id).id == a.id
    db_session.expire_all()
    stored = latest_assessment(db_session, db_session.info["test_company_id"], case.id)
    assert (stored.score, stored.tier, stored.triggered_reasons, stored.risk_rules_version_snapshot) == original

    # The old version row is untouched and still queryable.
    old = db_session.get(RiskRule, v1.id)
    assert (old.weight, old.version, old.is_active) == (30, 1, True)
    (snap_rule,) = stored.risk_rules_version_snapshot["rules"]
    assert snap_rule["version"] == 1 and snap_rule["weight"] == 30 and snap_rule["rule_pk"] == str(v1.id)

    # A NEW case scored afterwards uses the new weight, and records version 2.
    case2 = make_case(db_session, user)
    add_document(db_session, case2, user, "b.pdf", checks={T.metadata_forensics: _flag([finding("editing_software_detected")])})
    b = score_case(db_session, db_session.info["test_company_id"], case2.id)
    assert b.score == 5 and b.triggered_reasons[0]["rule_version"] == 2
    # ...while the first case still reads 30.
    assert latest_assessment(db_session, db_session.info["test_company_id"], case.id).score == 30


def test_current_rules_are_the_highest_version_only(db_session, rules):
    _new_version(db_session, "ela.tamper_region_detected", weight=1)
    _new_version(db_session, "ela.tamper_region_detected", weight=2)
    current = {r.rule_id: r for r in load_current_rules(db_session, db_session.info["test_company_id"], include_inactive=True)}
    assert current["ela.tamper_region_detected"].version == 3
    assert current["ela.tamper_region_detected"].weight == 2
    assert len(current) == len(SEED_RULES)
    assert db_session.query(RiskRule).filter_by(rule_id="ela.tamper_region_detected").count() == 3
