"""Reviewer workflow: approve / reject / escalate, visibility, audit trail."""
import uuid

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseTier
from app.models.case_action import CaseAction, CaseActionType
from app.models.document_check import DocumentCheckType as T
from app.models.user import User, UserRole
from app.services.risk_rule_seed import seed_risk_rules
from app.services.risk_scoring_service import score_case

from tests.helpers_risk import add_document, finding, make_case


def _flag(details):
    return {"result": "flag", "details": details}


TIER_CHECKS = {
    "low": {},
    "medium": {T.metadata_forensics: _flag([finding("editing_software_detected")])},  # 30
    "high": {
        T.metadata_forensics: _flag([finding("editing_software_detected")]),
        T.copy_move_detection: _flag([finding("copy_move_cluster")]),  # +35 = 65
    },
}


@pytest.fixture()
def scored_case(db_session, plain_user):
    """factory: a fully-analysed, scored case of the requested tier owned by `plain_user`."""
    seed_risk_rules(db_session, db_session.info["test_company_id"])
    db_session.commit()

    def make(tier="low", owner=None):
        case = make_case(db_session, owner or plain_user)
        add_document(db_session, case, owner or plain_user, "invoice.pdf", checks=TIER_CHECKS[tier])
        score_case(db_session, db_session.info["test_company_id"], case.id)
        db_session.commit()
        assert case.status == CaseStatus.pending_manual_review
        return case

    return make


def _events(db, case_id, event_type=None):
    stmt = select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.created_at)
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    return db.execute(stmt).scalars().all()


# ------------------------------------------------------------------- role gating

@pytest.mark.parametrize("action,body", [("approve", {}), ("reject", {"reason": "forged"}), ("escalate", {"reason": "x"})])
def test_submitters_cannot_act_on_cases(client, plain_headers, scored_case, action, body):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/{action}", json=body, headers=plain_headers).status_code == 403


@pytest.mark.parametrize("action", ["approve", "reject", "escalate"])
def test_actions_require_authentication(client, action):
    assert client.post(f"/cases/{uuid.uuid4()}/{action}", json={}).status_code == 401


def test_unknown_case_is_404(client, reviewer_headers):
    assert client.post(f"/cases/{uuid.uuid4()}/approve", json={}, headers=reviewer_headers).status_code == 404


def test_admin_can_act_too(client, auth_headers, scored_case):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/approve", json={}, headers=auth_headers).status_code == 200


# ----------------------------------------------------------------------- approve

def test_low_risk_case_approves_without_friction(client, reviewer_headers, reviewer_user, scored_case, db_session):
    case = scored_case("low")
    res = client.post(f"/cases/{case.id}/approve", json={}, headers=reviewer_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved" and body["action"]["action_type"] == "approve"
    assert body["action"]["actor_name"] == "Rita Reviewer"

    db_session.expire_all()
    assert db_session.get(Case, case.id).status == CaseStatus.approved
    (action,) = db_session.query(CaseAction).filter_by(case_id=case.id).all()
    assert (action.action_type, action.actor_user_id, action.notes) == (CaseActionType.approve, reviewer_user.id, None)
    (event,) = _events(db_session, case.id, "case_approved")
    assert event.actor_user_id == reviewer_user.id
    assert event.event_data["risk_tier"] == "low" and event.event_data["from_status"] == "pending_manual_review"


@pytest.mark.parametrize("tier", ["medium", "high"])
def test_flagged_case_needs_a_written_justification_to_approve(client, reviewer_headers, scored_case, db_session, tier):
    case = scored_case(tier)
    for note in (None, "", "   ", "ok"):
        res = client.post(f"/cases/{case.id}/approve", json={"note": note}, headers=reviewer_headers)
        assert res.status_code == 422 and tier in res.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Case, case.id).status == CaseStatus.pending_manual_review  # nothing changed

    note = "Verified the amounts directly with the vendor by phone."
    res = client.post(f"/cases/{case.id}/approve", json={"note": note}, headers=reviewer_headers)
    assert res.status_code == 200 and res.json()["action"]["notes"] == note
    (event,) = _events(db_session, case.id, "case_approved")
    assert event.event_data["note"] == note and event.event_data["risk_tier"] == tier
    assert event.event_data["risk_score"] >= 30


def test_cannot_approve_while_pipeline_is_still_running(client, reviewer_headers, db_session, plain_user):
    seed_risk_rules(db_session, db_session.info["test_company_id"])
    case = make_case(db_session, plain_user)
    add_document(db_session, case, plain_user, "a.pdf", complete=False)
    res = client.post(f"/cases/{case.id}/approve", json={}, headers=reviewer_headers)
    assert res.status_code == 409
    assert "still running" in res.json()["detail"] and "Text extraction" in res.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Case, case.id).status == CaseStatus.submitted


def test_cannot_approve_an_unscored_case_even_if_checks_finished(client, reviewer_headers, db_session, plain_user):
    case = make_case(db_session, plain_user)
    add_document(db_session, case, plain_user, "a.pdf")  # complete, but score_case never ran
    assert client.post(f"/cases/{case.id}/approve", json={}, headers=reviewer_headers).status_code == 409


def test_cannot_decide_twice(client, reviewer_headers, scored_case):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/approve", json={}, headers=reviewer_headers).status_code == 200
    again = client.post(f"/cases/{case.id}/approve", json={}, headers=reviewer_headers)
    assert again.status_code == 409 and "already approved" in again.json()["detail"]
    assert client.post(f"/cases/{case.id}/reject", json={"reason": "changed my mind"}, headers=reviewer_headers).status_code == 409


# ------------------------------------------------------------------------ reject

def test_reject_requires_a_reason_and_records_it(client, reviewer_headers, reviewer_user, scored_case, db_session):
    case = scored_case("high")
    for body in ({}, {"reason": ""}, {"reason": "   "}):
        assert client.post(f"/cases/{case.id}/reject", json=body, headers=reviewer_headers).status_code == 422
    db_session.expire_all()
    assert db_session.get(Case, case.id).status == CaseStatus.pending_manual_review

    res = client.post(f"/cases/{case.id}/reject", json={"reason": "Invoice total was altered."}, headers=reviewer_headers)
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    (event,) = _events(db_session, case.id, "case_rejected")
    assert event.actor_user_id == reviewer_user.id and event.event_data["reason"] == "Invoice total was altered."
    (action,) = db_session.query(CaseAction).filter_by(case_id=case.id).all()
    assert action.action_type == CaseActionType.reject and action.notes == "Invoice total was altered."


def test_a_reviewer_can_reject_before_the_pipeline_finishes(client, reviewer_headers, db_session, plain_user):
    case = make_case(db_session, plain_user)
    add_document(db_session, case, plain_user, "a.pdf", complete=False)
    res = client.post(f"/cases/{case.id}/reject", json={"reason": "Obvious forgery."}, headers=reviewer_headers)
    assert res.status_code == 200 and res.json()["status"] == "rejected"


# ---------------------------------------------------------------------- escalate

ESCALATE = {"reason": "Needs an L2 reviewer."}


def test_escalate_moves_case_to_l2_without_changing_status(client, reviewer_headers, reviewer_user, scored_case, db_session):
    case = scored_case("medium")
    res = client.post(f"/cases/{case.id}/escalate", json={"reason": "Needs a second pair of eyes."}, headers=reviewer_headers)
    assert res.status_code == 200
    body = res.json()
    assert (body["status"], body["assigned_tier"]) == ("pending_manual_review", "l2")
    assert body["action"]["actor_role"] == "Reviewer L1"
    db_session.expire_all()
    fresh = db_session.get(Case, case.id)
    assert (fresh.status, fresh.assigned_tier) == (CaseStatus.pending_manual_review, CaseTier.l2)
    (event,) = _events(db_session, case.id, "case_escalated")
    assert event.actor_user_id == reviewer_user.id and event.event_data["reason"] == "Needs a second pair of eyes."
    assert (event.event_data["from_tier"], event.event_data["to_tier"], event.event_data["actor_role"]) == ("l1", "l2", "reviewer_l1")
    assert not _events(db_session, case.id, "case_status_changed")[1:]  # only the automated review handoff


@pytest.mark.parametrize("body", [{}, {"reason": "   "}])
def test_escalate_requires_a_reason(client, reviewer_headers, scored_case, body):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/escalate", json=body, headers=reviewer_headers).status_code == 422


def test_escalate_twice_or_after_decision_is_rejected(client, reviewer_headers, l2_reviewer_headers, scored_case):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/escalate", json=ESCALATE, headers=l2_reviewer_headers).status_code == 200
    assert client.post(f"/cases/{case.id}/escalate", json=ESCALATE, headers=l2_reviewer_headers).status_code == 409
    other = scored_case("low")
    client.post(f"/cases/{other.id}/approve", json={}, headers=reviewer_headers)
    assert client.post(f"/cases/{other.id}/escalate", json=ESCALATE, headers=reviewer_headers).status_code == 409


def test_l1_loses_actions_but_keeps_read_access_on_an_escalated_case(client, reviewer_headers, scored_case):
    case = scored_case("low")
    assert client.post(f"/cases/{case.id}/escalate", json=ESCALATE, headers=reviewer_headers).status_code == 200

    for action, body in [("approve", {}), ("reject", {"reason": "forged"}), ("escalate", ESCALATE)]:
        assert client.post(f"/cases/{case.id}/{action}", json=body, headers=reviewer_headers).status_code == 403

    detail = client.get(f"/cases/{case.id}", headers=reviewer_headers)
    assert detail.status_code == 200
    assert detail.json()["assigned_tier"] == "l2" and detail.json()["can_act"] is False
    assert client.get(f"/cases/{case.id}/audit-log", headers=reviewer_headers).status_code == 200

    # Off the actionable queue, still listed (read-only) in the full view.
    actionable = {c["id"] for c in client.get("/cases?actionable=true", headers=reviewer_headers).json()}
    everything = {c["id"]: c for c in client.get("/cases", headers=reviewer_headers).json()}
    assert str(case.id) not in actionable
    assert everything[str(case.id)]["can_act"] is False


def test_l2_sees_and_acts_on_both_tiers(client, reviewer_headers, l2_reviewer_headers, scored_case):
    escalated, ordinary = scored_case("low"), scored_case("low")
    client.post(f"/cases/{escalated.id}/escalate", json=ESCALATE, headers=reviewer_headers)

    listed = client.get("/cases?actionable=true", headers=l2_reviewer_headers).json()
    assert {c["id"] for c in listed} == {str(escalated.id), str(ordinary.id)}
    assert all(c["can_act"] for c in listed)
    only_l2 = client.get("/cases?assigned_tier=l2", headers=l2_reviewer_headers).json()
    assert [c["id"] for c in only_l2] == [str(escalated.id)]

    res = client.post(f"/cases/{ordinary.id}/approve", json={}, headers=l2_reviewer_headers)
    assert res.status_code == 200 and res.json()["action"]["actor_role"] == "Reviewer L2"


def test_l2_resolves_an_escalated_case_directly(client, reviewer_headers, l2_reviewer_headers, scored_case, db_session):
    case = scored_case("high")
    client.post(f"/cases/{case.id}/escalate", json=ESCALATE, headers=reviewer_headers)
    res = client.post(f"/cases/{case.id}/approve", json={"note": "Verified with the vendor by phone."}, headers=l2_reviewer_headers)
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["assigned_tier"]) == ("approved", "l2")

    detail = client.get(f"/cases/{case.id}", headers=reviewer_headers).json()
    assert [(a["action_type"], a["actor_role"]) for a in detail["actions"]] == [
        ("escalate", "Reviewer L1"),
        ("approve", "Reviewer L2"),
    ]
    (event,) = _events(db_session, case.id, "case_approved")
    assert event.event_data["actor_role"] == "reviewer_l2"


def test_admin_can_act_on_escalated_cases(client, auth_headers, reviewer_headers, scored_case):
    case = scored_case("low")
    client.post(f"/cases/{case.id}/escalate", json=ESCALATE, headers=reviewer_headers)
    assert client.post(f"/cases/{case.id}/approve", json={}, headers=auth_headers).status_code == 200


def test_l1_queue_is_not_reordered_by_escalations(client, reviewer_headers, scored_case):
    first, second = scored_case("low"), scored_case("low")
    client.post(f"/cases/{first.id}/escalate", json=ESCALATE, headers=reviewer_headers)
    order = [c["id"] for c in client.get("/cases", headers=reviewer_headers).json()]
    assert order == [str(second.id), str(first.id)]  # newest first; L1 can't act on the escalated one


def test_escalated_cases_sort_to_the_top_of_the_l2_queue(client, reviewer_headers, l2_reviewer_headers, scored_case):
    first, second, third = scored_case("low"), scored_case("low"), scored_case("high")
    order = [c["id"] for c in client.get("/cases", headers=l2_reviewer_headers).json()]
    assert order == [str(third.id), str(second.id), str(first.id)]  # newest first by default

    client.post(f"/cases/{first.id}/escalate", json=ESCALATE, headers=reviewer_headers)
    listed = client.get("/cases", headers=l2_reviewer_headers).json()
    assert [c["id"] for c in listed][0] == str(first.id)
    top = listed[0]
    assert top["assigned_tier"] == "l2" and top["status"] == "pending_manual_review"  # status untouched


def test_a_decided_escalated_case_stops_floating_to_the_top(client, reviewer_headers, l2_reviewer_headers, scored_case):
    older, newer = scored_case("low"), scored_case("low")
    client.post(f"/cases/{older.id}/escalate", json=ESCALATE, headers=reviewer_headers)
    client.post(f"/cases/{older.id}/approve", json={}, headers=l2_reviewer_headers)
    assert [c["id"] for c in client.get("/cases", headers=l2_reviewer_headers).json()][0] == str(newer.id)


def test_renamed_reviewer_keeps_every_pre_migration_permission(client, db_session, reviewer_user, reviewer_headers, scored_case):
    """A migrated `reviewer` is a `reviewer_l1` and can still do everything a
    reviewer could: list/view any case, approve, reject, escalate, read the
    system audit log and export the report."""
    assert db_session.get(User, reviewer_user.id).role == UserRole.reviewer_l1
    approve_me, reject_me, escalate_me = scored_case("low"), scored_case("low"), scored_case("low")
    assert len(client.get("/cases", headers=reviewer_headers).json()) == 3
    assert client.get(f"/cases/{approve_me.id}", headers=reviewer_headers).status_code == 200
    assert client.post(f"/cases/{approve_me.id}/approve", json={}, headers=reviewer_headers).status_code == 200
    assert client.post(f"/cases/{reject_me.id}/reject", json={"reason": "forged"}, headers=reviewer_headers).status_code == 200
    assert client.post(f"/cases/{escalate_me.id}/escalate", json=ESCALATE, headers=reviewer_headers).status_code == 200
    assert client.get("/audit-log", headers=reviewer_headers).status_code == 200
    assert client.get("/settings/users", headers=reviewer_headers).status_code == 403  # never had admin


# --------------------------------------------------------- visibility & detail

def test_case_detail_exposes_assessment_pipeline_and_actions(client, reviewer_headers, scored_case):
    case = scored_case("high")
    client.post(f"/cases/{case.id}/escalate", json={"reason": "Urgent"}, headers=reviewer_headers)
    body = client.get(f"/cases/{case.id}", headers=reviewer_headers).json()

    assert body["assigned_tier"] == "l2" and body["status"] == "pending_manual_review"
    assert body["pipeline"] == {"complete": True, "pending": []}
    a = body["assessment"]
    assert a["tier"] == "high" and a["score"] == 65 and a["thresholds"] == {"medium": 30, "high": 60}
    assert {r["rule_id"] for r in a["triggered_reasons"]} == {"metadata.editing_software_detected", "copy_move.cluster_detected"}
    assert body["flag"]["flag"] == "high" and body["flag"]["score"] == 65
    assert [(x["action_type"], x["notes"]) for x in body["actions"]] == [("escalate", "Urgent")]


def test_submitters_only_see_their_own_cases(client, plain_headers, other_plain_headers, scored_case, plain_user, other_plain_user):
    mine = scored_case("low")
    theirs = scored_case("low", owner=other_plain_user)

    listed = {c["id"] for c in client.get("/cases", headers=plain_headers).json()}
    assert listed == {str(mine.id)}
    assert client.get(f"/cases/{mine.id}", headers=plain_headers).status_code == 200
    assert client.get(f"/cases/{theirs.id}", headers=plain_headers).status_code == 404  # 404, not 403
    assert client.get(f"/cases/{theirs.id}/audit-log", headers=plain_headers).status_code == 404


def test_submitter_sees_rejection_reason_but_not_escalation_notes(client, plain_headers, reviewer_headers, l2_reviewer_headers, scored_case):
    case = scored_case("high")
    client.post(f"/cases/{case.id}/escalate", json={"reason": "internal: suspect vendor"}, headers=reviewer_headers)
    client.post(f"/cases/{case.id}/reject", json={"reason": "Amount was altered."}, headers=l2_reviewer_headers)
    body = client.get(f"/cases/{case.id}", headers=plain_headers).json()
    assert body["status"] == "rejected"
    assert [(a["action_type"], a["notes"]) for a in body["actions"]] == [("reject", "Amount was altered.")]


# ---------------------------------------------------------------- system audit

def test_every_action_appears_in_the_system_audit_log(client, reviewer_headers, reviewer_user, scored_case):
    approved, rejected, escalated = scored_case("low"), scored_case("high"), scored_case("medium")
    client.post(f"/cases/{approved.id}/approve", json={}, headers=reviewer_headers)
    client.post(f"/cases/{rejected.id}/reject", json={"reason": "Forged stamp."}, headers=reviewer_headers)
    client.post(f"/cases/{escalated.id}/escalate", json={"reason": "Check with finance."}, headers=reviewer_headers)

    page = client.get("/audit-log?limit=500", headers=reviewer_headers).json()
    by_type = {}
    for e in page["items"]:
        by_type.setdefault(e["event_type"], []).append(e)
    for event_type, case, expected in [
        ("case_approved", approved, None),
        ("case_rejected", rejected, "Forged stamp."),
        ("case_escalated", escalated, "Check with finance."),
    ]:
        (e,) = by_type[event_type]
        assert e["case_id"] == str(case.id) and e["case_number"] == case.case_number
        assert e["actor_name"] == "Rita Reviewer" and e["actor_email"] == reviewer_user.email
        assert e["created_at"]
        if expected:
            assert e["event_data"].get("reason") == expected

    only = client.get("/audit-log?event_type=case_rejected", headers=reviewer_headers).json()
    assert only["total"] == 1 and only["items"][0]["event_type"] == "case_rejected"
    by_case = client.get(f"/audit-log?case_id={approved.id}", headers=reviewer_headers).json()
    assert {e["case_id"] for e in by_case["items"]} == {str(approved.id)}
    assert client.get(f"/audit-log?q={rejected.case_number}", headers=reviewer_headers).json()["total"] >= 1
    assert "case_approved" in client.get("/audit-log/event-types", headers=reviewer_headers).json()


def test_audit_log_is_not_available_to_submitters(client, plain_headers):
    assert client.get("/audit-log", headers=plain_headers).status_code == 403
    assert client.get("/audit-log/event-types", headers=plain_headers).status_code == 403
    assert client.get("/audit-log").status_code == 401


def test_audit_log_pagination(client, reviewer_headers, scored_case):
    scored_case("low"), scored_case("low")
    everything = client.get("/audit-log?limit=500", headers=reviewer_headers).json()
    page = client.get("/audit-log?limit=2&offset=1", headers=reviewer_headers).json()
    assert page["total"] == everything["total"] and len(page["items"]) == 2
    assert [e["id"] for e in page["items"]] == [e["id"] for e in everything["items"][1:3]]


def test_submitters_never_see_their_own_risk_tier_score_or_reasons(client, plain_headers, reviewer_headers, scored_case):
    case = scored_case("high")
    for body in (
        client.get(f"/cases/{case.id}", headers=plain_headers).json(),
        next(c for c in client.get("/cases", headers=plain_headers).json() if c["id"] == str(case.id)),
    ):
        assert body["risk_tier"] is None
        assert body["flag"] == {"flag": "pending", "label": "In review", "description": "Your case is with the review team.", "score": None}
    assert client.get(f"/cases/{case.id}", headers=plain_headers).json()["assessment"] is None

    # ...while a reviewer sees all of it for the very same case.
    seen = client.get(f"/cases/{case.id}", headers=reviewer_headers).json()
    assert seen["risk_tier"] == "high" and seen["assessment"]["score"] == 65 and seen["flag"]["flag"] == "high"
