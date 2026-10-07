"""
Unit tests for app/services/case_flag_service.py. Uses the shared
`db_session` fixture (in-memory SQLite, see tests/conftest.py).

The case flag is the case's REAL risk tier from its latest
case_risk_assessments row (low/medium/high), or "pending" until scored —
see the module docstring. get_forensic_findings is unchanged.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.models.case import Case, CaseStatus, CaseType, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.user import User, UserRole
from app.services.case_flag_service import get_case_flag, get_case_flags, get_forensic_findings

def _seed_user(db_session) -> User:
    user = User(
        email="uploader@example.com", hashed_password="x", role=UserRole.user, is_active=True
    )
    db_session.add(user)
    db_session.flush()
    return user


def _seed_case(db_session, user, *, document_count: int, case_number: str = "CASE-FLAGTEST") -> Case:
    case = Case(
        case_number=case_number,
        case_type=CaseType.vendor_invoice,
        submitted_by_user_id=user.id,
        status=CaseStatus.submitted,
    )
    db_session.add(case)
    db_session.flush()
    for i in range(document_count):
        db_session.add(
            Document(
                case_id=case.id,
                uploaded_by_user_id=user.id,
                original_filename=f"doc{i}.pdf",
                blob_storage_path=f"https://fake/{i}",
                file_hash=str(i) * 64,
            )
        )
    db_session.commit()
    return case

def _add_check(db_session, document, check_type, status, result=None):
    db_session.add(
        DocumentCheck(document_id=document.id, check_type=check_type, status=status, result=result)
    )
    db_session.commit()


def _documents_for(db_session, case):
    return db_session.query(Document).filter(Document.case_id == case.id).order_by(Document.original_filename).all()


def _assess(db_session, case, tier, score, reasons=None, minutes_ago=0):
    db_session.add(
        CaseRiskAssessment(
            case_id=case.id,
            raw_score=score,
            score=score,
            tier=tier,
            triggered_reasons=reasons or [],
            risk_rules_version_snapshot={"rules": [], "thresholds": {"medium": 30, "high": 60}},
            evidence_fingerprint=f"fp-{uuid.uuid4()}",
            computed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
    )
    db_session.commit()


def test_unscored_case_is_pending(db_session):
    case = _seed_case(db_session, _seed_user(db_session), document_count=1)
    flag = get_case_flag(db_session, db_session.info["test_company_id"], case.id)
    assert flag.flag == "pending"
    assert flag.score is None


def test_single_document_case_gets_a_real_tier(db_session):
    """One-document cases run forensics too — they get a tier, not
    'evidence required'."""
    case = _seed_case(db_session, _seed_user(db_session), document_count=1)
    _assess(db_session, case, RiskTier.high, 72, [{"reason": "Editing software found."}])
    flag = get_case_flag(db_session, db_session.info["test_company_id"], case.id)
    assert flag.flag == "high"
    assert flag.label == "High risk"
    assert flag.score == 72
    assert flag.description == "Editing software found."


def test_flag_uses_latest_assessment_and_counts_extra_signals(db_session):
    case = _seed_case(db_session, _seed_user(db_session), document_count=2)
    _assess(db_session, case, RiskTier.high, 80, [{"reason": "old"}], minutes_ago=30)
    _assess(db_session, case, RiskTier.low, 5, [{"reason": r} for r in "abcde"])
    flag = get_case_flag(db_session, db_session.info["test_company_id"], case.id)
    assert flag.flag == "low"
    assert flag.description == "a · b · c (+2 more)"


def test_flag_lists_short_rule_titles(db_session):
    case = _seed_case(db_session, _seed_user(db_session), document_count=1)
    _assess(db_session, case, RiskTier.high, 90, [
        {"rule_id": "font.inconsistency", "reason": "'x.pdf': 17 piece(s) of text ..."},
        {"rule_id": "content.deleted_ghost_block", "reason": "'x.pdf': 1 block(s) ..."},
    ])
    flag = get_case_flag(db_session, db_session.info["test_company_id"], case.id)
    assert flag.description == "Fonts: text retyped · Text deleted after conversion"


def test_clean_scored_case_says_no_signals(db_session):
    case = _seed_case(db_session, _seed_user(db_session), document_count=2)
    _assess(db_session, case, RiskTier.low, 0, [])
    flag = get_case_flag(db_session, db_session.info["test_company_id"], case.id)
    assert flag.flag == "low"
    assert "No risk signals" in flag.description


def test_get_case_flags_batches_multiple_cases(db_session):
    user = _seed_user(db_session)
    a = _seed_case(db_session, user, document_count=1, case_number="CASE-A")
    b = _seed_case(db_session, user, document_count=1, case_number="CASE-B")
    c = _seed_case(db_session, user, document_count=1, case_number="CASE-C")
    _assess(db_session, a, RiskTier.medium, 40)
    _assess(db_session, b, RiskTier.high, 90)
    flags = get_case_flags(db_session, db_session.info["test_company_id"], [a.id, b.id, c.id])
    assert [flags[a.id].flag, flags[b.id].flag, flags[c.id].flag] == ["medium", "high", "pending"]


def test_get_case_flags_empty_input_returns_empty_dict(db_session):
    assert get_case_flags(db_session, db_session.info["test_company_id"], []) == {}


def test_unknown_case_id_is_pending(db_session):
    assert get_case_flag(db_session, db_session.info["test_company_id"], uuid.uuid4()).flag == "pending"


def test_get_forensic_findings_filters_to_material_severity(db_session):
    user = _seed_user(db_session)
    case = _seed_case(db_session, user, document_count=1)
    doc = _documents_for(db_session, case)[0]
    _add_check(
        db_session,
        doc,
        DocumentCheckType.metadata_forensics,
        DocumentCheckStatus.completed,
        result={
            "result": "flag",
            "details": [
                {"finding": "info_dictionary", "severity": "info", "description": "Info dictionary has 5 field(s)."},
                {"finding": "editing_software_detected", "severity": "high", "description": "Producer mentions Photoshop."},
            ],
        },
    )
    db_session.refresh(doc)

    findings = get_forensic_findings([doc])
    assert len(findings) == 1
    assert findings[0].finding == "editing_software_detected"
    assert findings[0].document_filename == doc.original_filename
    assert findings[0].check_type == "metadata_forensics"

def test_duplicate_check_findings_are_listed_as_forensic_findings(db_session):
    user = _seed_user(db_session)
    case = _seed_case(db_session, user, document_count=1)
    doc = _documents_for(db_session, case)[0]
    _add_check(
        db_session,
        doc,
        DocumentCheckType.duplicate_detection,
        DocumentCheckStatus.completed,
        result={
            "result": "flag",
            "details": [
                {
                    "finding": "near_duplicate_page",
                    "severity": "high",
                    "description": "Page 1 is a near-identical match to a previously submitted document.",
                    "page": 1,
                    "data": {"matched_document_id": "x", "matched_page": 1, "distance": 2},
                }
            ],
        },
    )
    db_session.refresh(doc)
    findings = get_forensic_findings([doc])
    assert len(findings) == 1
    assert findings[0].check_type == "duplicate_detection"
    assert findings[0].finding == "near_duplicate_page"


def test_get_forensic_findings_ignores_non_flagged_checks(db_session):
    user = _seed_user(db_session)
    case = _seed_case(db_session, user, document_count=1)
    doc = _documents_for(db_session, case)[0]
    _add_check(
        db_session,
        doc,
        DocumentCheckType.copy_move_detection,
        DocumentCheckStatus.completed,
        result={"result": "pass", "details": []},
    )
    db_session.refresh(doc)

    assert get_forensic_findings([doc]) == []
