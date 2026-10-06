"""
Case workflow (SPECIFICATION.md section 3.4): a small `transitions` state machine
plus an internal event emitter.

Status model used in v1:

    submitted / under_automated_review
        --ready_for_review-->  pending_manual_review   (set when scoring completes)
    pending_manual_review  --approve-->  approved
    submitted / under_automated_review / pending_manual_review
                           --reject-->   rejected

Escalation is NOT a state. It moves `Case.assigned_tier` from "l1" to "l2",
handing the case to the L2 reviewer tier (who can act on it; an L1 reviewer
keeps read-only access). It leaves `status` untouched, can only be applied
to a case not yet decided, and is one-directional — there is no
de-escalation; L2 resolves the case with the ordinary approve/reject.

The other CaseStatus values (auto_approved, escalated, under_investigation,
closed) stay in the enum for the eventual fuller workflow but nothing in v1
transitions into them.

EVENTS (SPECIFICATION.md section 4): transitions do not call side effects
directly; they `emit()` a WorkflowEvent, and subscribers react. The audit
log is the current subscriber, so every event lands in `audit_log` — the
same table the Audit History screen reads. A future external case-
management integration is just another `subscribe()` call.
  - case_status_changed : an automated transition (-> pending_manual_review)
  - case_approved / case_rejected : a reviewer decision. These ARE the
    status-change events for their transitions, so they are not followed by
    a duplicate case_status_changed.
  - case_escalated : tier change l1 -> l2 (no status change).

Every event carries the case's `company_id`, so any subscriber (today the
audit log; later perhaps an external case-management integration) stays
tenant-aware without looking the case up again.

Each human decision also writes a `case_actions` row — the reviewer-facing
action history shown on the case detail page.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session
from transitions import Machine, MachineError

from app.models.case import Case, CaseStatus, CaseTier
from app.models.case_action import CaseAction, CaseActionType
from app.models.user import User
from app.services.audit_service import record_event


class InvalidCaseTransition(Exception):
    """The requested action isn't valid from the case's current state."""


# ---------------------------------------------------------------------------
# Event emitter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkflowEvent:
    name: str
    case_id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)
    company_id: uuid.UUID | None = None


Subscriber = Callable[[Session, WorkflowEvent], None]
_subscribers: list[Subscriber] = []


def subscribe(handler: Subscriber) -> None:
    if handler not in _subscribers:
        _subscribers.append(handler)


def emit(db: Session, event: WorkflowEvent) -> None:
    for handler in list(_subscribers):
        handler(db, event)


def _audit_subscriber(db: Session, event: WorkflowEvent) -> None:
    record_event(
        db,
        event.name,
        case_id=event.case_id,
        actor_user_id=event.actor_user_id,
        event_data=event.data,
        company_id=event.company_id,
    )


subscribe(_audit_subscriber)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

_TRANSITIONS = [
    {
        "trigger": "ready_for_review",
        "source": [CaseStatus.submitted.value, CaseStatus.under_automated_review.value],
        "dest": CaseStatus.pending_manual_review.value,
    },
    {
        "trigger": "approve",
        "source": CaseStatus.pending_manual_review.value,
        "dest": CaseStatus.approved.value,
    },
    {
        # A reviewer can reject at any open stage — e.g. an obvious forgery
        # needn't wait for the rest of the pipeline. (Approve, by contrast,
        # only ever happens after review.)
        "trigger": "reject",
        "source": [
            CaseStatus.submitted.value,
            CaseStatus.under_automated_review.value,
            CaseStatus.pending_manual_review.value,
        ],
        "dest": CaseStatus.rejected.value,
    },
]


class _StateHolder:
    """The object `transitions` drives; mirrors Case.status."""

    def __init__(self) -> None:
        self.state: str = ""


def _apply(case: Case, trigger: str) -> tuple[CaseStatus, CaseStatus]:
    """Run `trigger` through the state machine against the case's current
    status; returns (old, new) and sets `case.status`. Raises
    InvalidCaseTransition if the trigger isn't allowed from that status."""
    holder = _StateHolder()
    Machine(
        model=holder,
        states=[s.value for s in CaseStatus],
        transitions=_TRANSITIONS,
        initial=case.status.value,
        auto_transitions=False,
    )
    old = case.status
    try:
        getattr(holder, trigger)()
    except MachineError as exc:
        raise InvalidCaseTransition(
            f"Cannot {trigger.replace('_', ' ')} a case that is '{old.value}'."
        ) from exc
    case.status = CaseStatus(holder.state)
    return old, case.status


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def mark_ready_for_review(db: Session, case: Case) -> None:
    old, new = _apply(case, "ready_for_review")
    emit(
        db,
        WorkflowEvent(
            "case_status_changed",
            case.id,
            None,
            {"from_status": old.value, "to_status": new.value, "reason": "automated checks complete"},
            company_id=case.company_id,
        ),
    )


def _record_action(
    db: Session, case: Case, actor: User, action_type: CaseActionType, notes: str | None
) -> CaseAction:
    action = CaseAction(
        company_id=case.company_id,
        case_id=case.id,
        actor_user_id=actor.id,
        actor_role=actor.role.value,
        action_type=action_type,
        notes=notes,
    )
    db.add(action)
    return action


def approve_case(
    db: Session,
    case: Case,
    actor: User,
    *,
    note: str | None,
    risk_tier: str | None,
    risk_score: int | None,
    assessment_id: uuid.UUID | None,
) -> CaseAction:
    old, new = _apply(case, "approve")
    action = _record_action(db, case, actor, CaseActionType.approve, note)
    emit(
        db,
        WorkflowEvent(
            "case_approved",
            case.id,
            actor.id,
            {
                "from_status": old.value,
                "to_status": new.value,
                "actor_role": actor.role.value,
                "note": note,
                "risk_tier": risk_tier,
                "risk_score": risk_score,
                "assessment_id": str(assessment_id) if assessment_id else None,
            },
            company_id=case.company_id,
        ),
    )
    return action


def reject_case(db: Session, case: Case, actor: User, *, reason: str) -> CaseAction:
    old, new = _apply(case, "reject")
    action = _record_action(db, case, actor, CaseActionType.reject, reason)
    emit(
        db,
        WorkflowEvent(
            "case_rejected",
            case.id,
            actor.id,
            {"from_status": old.value, "to_status": new.value, "actor_role": actor.role.value, "reason": reason},
            company_id=case.company_id,
        ),
    )
    return action


def escalate_case(db: Session, case: Case, actor: User, *, reason: str) -> CaseAction:
    if case.status in (CaseStatus.approved, CaseStatus.rejected, CaseStatus.closed):
        raise InvalidCaseTransition(f"Cannot escalate a case that is '{case.status.value}'.")
    if case.assigned_tier == CaseTier.l2:
        raise InvalidCaseTransition("This case is already escalated to L2.")
    old_tier = case.assigned_tier
    case.assigned_tier = CaseTier.l2
    action = _record_action(db, case, actor, CaseActionType.escalate, reason)
    emit(
        db,
        WorkflowEvent(
            "case_escalated",
            case.id,
            actor.id,
            {
                "status": case.status.value,
                "from_tier": old_tier.value,
                "to_tier": case.assigned_tier.value,
                "actor_role": actor.role.value,
                "reason": reason,
            },
            company_id=case.company_id,
        ),
    )
    return action
