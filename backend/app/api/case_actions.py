"""
Reviewer workflow actions on a case: approve, reject, escalate.

Only the company's reviewers (L1/L2) may act (403 for `user` and for platform
admins, whose access is read-only). A case of another company is a 404. A case's
`assigned_tier` narrows that further: once escalated to L2, a `reviewer_l1`
gets 403 on every action here (they can still GET it — see
app/api/case_access.py). All rules are enforced HERE, server-side — the UI's
disabled buttons and confirmation step are conveniences, not the guard:

  approve   - only once the automated pipeline has finished AND a risk
              assessment exists (never approve a half-checked case). No hard
              block on a high-risk case (a reviewer may have verified
              something the system couldn't), but anything above "low"
              requires a written justification, which is stored.
  reject    - a reason is required.
  escalate  - moves the case to the L2 tier (`assigned_tier` = "l2"); it never
              changes `status`. One-directional: there is no de-escalation,
              an L2 reviewer resolves it with approve/reject. A reason is
              required.

Every action writes a `case_actions` row (the reviewer-facing history) and
an `audit_log` row (via the workflow event subscriber) — the same table the
Audit History screen reads.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_tenant_db, require_company_role
from app.api.case_access import ensure_can_act, scoped_company_id
from app.models.case import Case, CaseStatus, RiskTier
from app.models.case_action import CaseAction
from app.models.user import User, UserRole, role_label
from app.schemas.case import (
    ApproveRequest,
    CaseActionSchema,
    CaseDecisionResponse,
    EscalateRequest,
    RejectRequest,
)
from app.services import workflow_service
from app.services.risk_scoring_service import latest_assessment, pipeline_status
from app.services.workflow_service import InvalidCaseTransition

router = APIRouter(prefix="/cases", tags=["case-actions"])

_reviewer = require_company_role(UserRole.reviewer_l1)

# Minimum length of the justification required to approve a flagged case —
# enough to force a real sentence, not a keystroke.
_MIN_JUSTIFICATION_CHARS = 10


def _get_case(db: Session, case_id: uuid.UUID, actor: User) -> Case:
    """The case, 404 if missing or in another company, 403 if its tier is
    above the actor's."""
    case = db.execute(
        select(Case).where(Case.id == case_id, Case.company_id == scoped_company_id(db))
    ).scalar_one_or_none()
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    ensure_can_act(actor, case)
    return case


def _decision(db: Session, case: Case, action: CaseAction, actor: User) -> CaseDecisionResponse:
    db.commit()
    db.refresh(case)
    db.refresh(action)
    return CaseDecisionResponse(
        case_id=case.id,
        status=case.status,
        assigned_tier=case.assigned_tier,
        action=CaseActionSchema(
            id=action.id,
            action_type=action.action_type.value,
            actor_name=actor.full_name or actor.email,
            actor_role=role_label(action.actor_role),
            notes=action.notes,
            created_at=action.created_at,
        ),
    )


@router.post(
    "/{case_id}/approve",
    response_model=CaseDecisionResponse,
    summary="Approve a case",
    description=(
        "Company reviewers only (platform admins: 403). Requires the automated pipeline to have finished and a risk "
        "assessment to exist (409 otherwise). Above `low` risk a written justification (`note`, "
        "at least 10 characters) is required (422). Approving an already-decided case is a 409. "
        "Writes a `case_actions` row and an audit event."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a company reviewer role; a `reviewer_l1` also gets 403 on a case escalated to L2."},
        404: {"description": "No such case in your company."},
        409: {"description": "Case already decided, checks still running, or an invalid state transition."},
        422: {"description": "Case is above low risk and no justification of at least 10 characters was given."},
    },
)
def approve_case(
    case_id: uuid.UUID,
    payload: ApproveRequest,
    actor: User = Depends(_reviewer),
    db: Session = Depends(get_tenant_db),
) -> CaseDecisionResponse:
    case = _get_case(db, case_id, actor)

    if case.status in (CaseStatus.approved, CaseStatus.rejected, CaseStatus.closed):
        raise HTTPException(status.HTTP_409_CONFLICT, f"This case is already {case.status.value}.")

    pipeline = pipeline_status(db, case.company_id, case.id)
    assessment = latest_assessment(db, case.company_id, case.id)
    if not pipeline.complete or assessment is None:
        detail = "Automated checks are still running, so this case can't be approved yet."
        if pipeline.pending:
            detail += " Waiting on: " + "; ".join(pipeline.pending[:5]) + "."
        raise HTTPException(status.HTTP_409_CONFLICT, detail)

    note = (payload.note or "").strip() or None
    if assessment.tier != RiskTier.low and (note is None or len(note) < _MIN_JUSTIFICATION_CHARS):
        raise HTTPException(
            422,
            f"This case is {assessment.tier.value} risk. Enter a justification of at least "
            f"{_MIN_JUSTIFICATION_CHARS} characters to approve it.",
        )

    try:
        action = workflow_service.approve_case(
            db,
            case,
            actor,
            note=note,
            risk_tier=assessment.tier.value,
            risk_score=assessment.score,
            assessment_id=assessment.id,
        )
    except InvalidCaseTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _decision(db, case, action, actor)


@router.post(
    "/{case_id}/reject",
    response_model=CaseDecisionResponse,
    summary="Reject a case",
    description=(
        "Company reviewers only (platform admins: 403). A `reason` is required. Rejecting an already-decided case is a "
        "409. Writes a `case_actions` row and an audit event."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a company reviewer role; a `reviewer_l1` also gets 403 on a case escalated to L2."},
        404: {"description": "No such case in your company."},
        409: {"description": "Invalid state transition (e.g. already decided)."},
    },
)
def reject_case(
    case_id: uuid.UUID,
    payload: RejectRequest,
    actor: User = Depends(_reviewer),
    db: Session = Depends(get_tenant_db),
) -> CaseDecisionResponse:
    case = _get_case(db, case_id, actor)
    try:
        action = workflow_service.reject_case(db, case, actor, reason=payload.reason)
    except InvalidCaseTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _decision(db, case, action, actor)


@router.post(
    "/{case_id}/escalate",
    response_model=CaseDecisionResponse,
    summary="Escalate a case",
    description=(
        "Company reviewers only (platform admins: 403). Moves the case to the L2 tier (`assigned_tier` = `l2`); `status` is "
        "unchanged. A `reviewer_l1` then keeps read access but can no longer act on it. There is "
        "no de-escalation — an L2 reviewer approves or rejects it. A `reason` is required. "
        "Escalating an already-escalated or decided case is a 409."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a company reviewer role; a `reviewer_l1` also gets 403 on a case escalated to L2."},
        404: {"description": "No such case in your company."},
        409: {"description": "Invalid state transition (e.g. already decided)."},
    },
)
def escalate_case(
    case_id: uuid.UUID,
    payload: EscalateRequest,
    actor: User = Depends(_reviewer),
    db: Session = Depends(get_tenant_db),
) -> CaseDecisionResponse:
    case = _get_case(db, case_id, actor)
    try:
        action = workflow_service.escalate_case(db, case, actor, reason=payload.reason)
    except InvalidCaseTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _decision(db, case, action, actor)
