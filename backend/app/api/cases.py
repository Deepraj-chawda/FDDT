"""
Case management endpoints: create, list (the case queue), and detail.

Tenancy: every route is confined to one company — the caller's own, or, for
a platform admin (read-only support access), the company of the case being
read / the `company_id` chosen for the list, with that access audited (see
app/api/tenant_access.py). A case of another company is a 404.

Visibility within the company: every reviewer (L1/L2) sees every case; a
`user` (submitter) only sees the cases they submitted — a case that isn't
theirs is a 404, not a 403, so ids can't be probed. Each case also carries `can_act`,
the tier rule from app/api/case_access.py: a `reviewer_l1` can view an
L2-escalated case but not act on it. The reviewer actions (approve/reject/
escalate) live in app/api/case_actions.py.
"""
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case as sql_case, false, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.auth import get_tenant_db, require_company_role
from app.api.case_access import can_act_on_case, load_visible_case
from app.api.tenant_access import CaseScope, CompanyScope, company_scope, get_case_scope
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseTier, CaseType
from app.models.case_action import CaseAction, CaseActionType
from app.models.document import Document
from app.models.user import User, UserRole, has_rank, role_label
from app.schemas.case import (
    AuditLogEntrySchema,
    CaseActionSchema,
    PipelineStatusSchema,
    RiskAssessmentSchema,
    CaseCreateRequest,
    CaseDetail,
    CaseFlagSchema,
    CaseListItem,
    CaseResponse,
    CrossDocumentFindingSummary,
    ForensicFindingSummary,
    UserSummary,
)
from app.schemas.document import CaseDocumentSummary
from app.services.audit_service import record_event
from app.services.case_flag_service import get_case_flag, get_case_flags, get_forensic_findings
from app.services.risk_scoring_service import latest_assessment, pipeline_status
from app.services.storage_service import StorageService, ensure_company_blob, get_storage_service
from app.services.usage_service import record_case_created

router = APIRouter(prefix="/cases", tags=["cases"])

# What a submitter's case timeline must not show (GET /cases/{id}/audit-log):
# the scoring event itself, and the result details of automated checks — a
# bad actor must not learn which signals fired.
_SUBMITTER_HIDDEN_EVENTS = frozenset({"risk_assessment_completed"})
_SUBMITTER_HIDDEN_KEYS = frozenset({
    "result", "ela_result", "copy_move_result", "finding_count", "ela_finding_count",
    "copy_move_finding_count", "match_count", "regions_detected", "tier", "score", "raw_score",
    "rules_fired", "assessment_id",
})

_DECIDED = (CaseStatus.approved, CaseStatus.rejected, CaseStatus.closed)


def _visible_flag(user: User, flag: CaseFlagSchema) -> CaseFlagSchema:
    """Reviewers/admins get the real risk tier + score. A submitter does NOT:
    seeing which signals fired (and how heavily) on their own submission
    would tell a bad actor exactly what to evade. They only get a neutral
    "in review" marker; the reviewer's decision and reasons still reach them
    via the case's approve/reject actions."""
    if user.role != UserRole.user:  # reviewers and platform admins
        return flag
    return CaseFlagSchema(
        flag="pending",
        label="In review",
        description="Your case is with the review team.",
        score=None,
    )


_load_visible_case = load_visible_case  # shared rule lives in app/api/case_access.py


@router.post(
    "",
    response_model=CaseResponse,
    status_code=201,
    summary="Create a case",
    description=(
        "Opens an empty case (status `submitted`) owned by the caller, in the caller's company; "
        "any company role may create one (platform admins may not — 403). The case number is `CASE-` plus the first 8 hex characters of the case "
        "UUID. Attach documents afterwards with POST /cases/{case_id}/documents. Writes a "
        "`case_created` audit event."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Platform admins cannot create cases."},
    },
)
def create_case(
    payload: CaseCreateRequest,
    current_user: User = Depends(require_company_role(UserRole.user)),
    db: Session = Depends(get_tenant_db),
) -> Case:
    case_id = uuid.uuid4()
    case = Case(
        id=case_id,
        company_id=current_user.company_id,
        # Derived from the case's own id, so it's unique for free and needs
        # no separate sequence/counter table.
        case_number=f"CASE-{case_id.hex[:8].upper()}",
        case_type=payload.case_type,
        submitted_by_user_id=current_user.id,
        status=CaseStatus.submitted,
    )
    db.add(case)
    # `AuditLog` has no ORM `relationship()` to `Case` (deliberately kept
    # decoupled), so the unit-of-work flush order isn't derived from an
    # object graph here — flush the case row first so the audit_log
    # insert's FK reference is satisfied.
    db.flush()

    record_event(
        db,
        "case_created",
        case_id=case.id,
        actor_user_id=current_user.id,
        event_data={"case_type": payload.case_type.value},
    )
    record_case_created(db, current_user.company_id)

    db.commit()
    db.refresh(case)
    return case


@router.get(
    "",
    response_model=list[CaseListItem],
    summary="List cases (the case queue)",
    description=(
        "Only the caller's company. Reviewers see every case of their company; a `user` sees "
        "only the cases they submitted. A platform admin must pass `company_id` (one company "
        "at a time, audited) — there is no cross-company case list. "
        "For `reviewer_l2` and platform admins, open cases escalated to L2 sort first; otherwise newest "
        "first. Optional filters: `status`, `case_type`, `assigned_tier`, and `actionable=true` "
        "(only cases the caller may act on — for a `reviewer_l1` that excludes L2 cases). Each "
        "item carries `can_act`. For submitters `risk_tier` is null and `flag` is a neutral "
        "'In review' marker, so scoring signals are never disclosed to them."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        400: {"description": "A platform admin did not pass `company_id`."},
        404: {"description": "`company_id` names an unknown company (or, for a company user, not theirs)."},
    },
)
def list_cases(
    status_filter: CaseStatus | None = Query(default=None, alias="status"),
    case_type_filter: CaseType | None = Query(default=None, alias="case_type"),
    tier_filter: CaseTier | None = Query(default=None, alias="assigned_tier"),
    actionable: bool = Query(
        default=False, description="Only cases the caller may approve/reject/escalate."
    ),
    scope: CompanyScope = Depends(company_scope("case list")),
) -> list[CaseListItem]:
    db, current_user = scope.db, scope.ctx.user
    stmt = (
        select(Case, func.count(Document.id))
        .outerjoin(Document, Document.case_id == Case.id)
        .options(selectinload(Case.submitted_by))
        .where(Case.company_id == scope.company_id)
        .group_by(Case.id)
    )
    if current_user.is_platform_admin or has_rank(current_user.role, UserRole.reviewer_l2):
        # Surfacing escalations is the point of the L2 tier: open L2 cases
        # sort to the top (a CASE expression, not an enum sort: enum
        # ordering differs by database).
        stmt = stmt.order_by(
            sql_case((and_(Case.assigned_tier == CaseTier.l2, Case.status.notin_(_DECIDED)), 0), else_=1)
        )
    stmt = stmt.order_by(Case.created_at.desc())
    if current_user.role == UserRole.user:
        stmt = stmt.where(Case.submitted_by_user_id == current_user.id)
    if actionable:
        if current_user.role == UserRole.user or current_user.is_platform_admin:
            stmt = stmt.where(false())
        elif not has_rank(current_user.role, UserRole.reviewer_l2):
            stmt = stmt.where(Case.assigned_tier == CaseTier.l1)
    if tier_filter is not None:
        stmt = stmt.where(Case.assigned_tier == tier_filter)
    if status_filter is not None:
        stmt = stmt.where(Case.status == status_filter)
    if case_type_filter is not None:
        stmt = stmt.where(Case.case_type == case_type_filter)

    rows = db.execute(stmt).all()
    flags_by_case = get_case_flags(db, scope.company_id, [case.id for case, _ in rows])
    return [
        CaseListItem(
            id=case.id,
            case_number=case.case_number,
            case_type=case.case_type,
            status=case.status,
            assigned_tier=case.assigned_tier,
            risk_tier=case.risk_tier if current_user.role != UserRole.user else None,
            submitted_by=UserSummary.model_validate(case.submitted_by) if case.submitted_by else None,
            created_at=case.created_at,
            document_count=document_count,
            flag=_visible_flag(current_user, CaseFlagSchema.from_case_flag(flags_by_case[case.id])),
            can_act=can_act_on_case(current_user, case),
        )
        for case, document_count in rows
    ]


@router.get(
    "/{case_id}",
    response_model=CaseDetail,
    summary="Get case detail",
    description=(
        "Everything the case detail page needs in one call: documents (with signed download "
        "URLs, extracted fields and per-document check results), cross-document findings, "
        "forensic findings, the latest risk assessment, pipeline progress (`pipeline.complete` "
        "/ `pipeline.pending`) and reviewer actions. A submitter does not receive the risk "
        "assessment and sees only approve/reject actions. A case of another company, or owned "
        "by someone else, is a 404, not a 403, so ids cannot be probed. A platform admin may "
        "read any company's case; each such access is written to that company's audit log."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "No such case (or, for a `user`, not one they submitted)."},
    },
)
def get_case_detail(
    case_id: uuid.UUID,
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> CaseDetail:
    db, current_user = scope.db, scope.ctx.user
    case = _load_visible_case(
        db,
        case_id,
        current_user,
        selectinload(Case.submitted_by),
        selectinload(Case.documents).selectinload(Document.checks),
        selectinload(Case.cross_document_findings),
    )

    def _signed(doc: Document) -> str:
        ensure_company_blob(doc.blob_storage_path, case.company_id)
        return storage.get_download_url(doc.blob_storage_path)

    documents = [
        CaseDocumentSummary.from_document(doc, _signed(doc))
        for doc in sorted(case.documents, key=lambda d: d.created_at)
    ]
    documents_by_id = {str(d.id): d for d in case.documents}
    cross_document_findings = [
        CrossDocumentFindingSummary.from_finding(finding, documents_by_id)
        for finding in sorted(case.cross_document_findings, key=lambda f: f.created_at)
    ]
    forensic_findings = [
        ForensicFindingSummary.from_finding(finding)
        for finding in get_forensic_findings(case.documents)
    ]

    assessment = latest_assessment(db, case.company_id, case.id)
    pipeline = pipeline_status(db, case.company_id, case.id)

    # Reviewers see every action; a submitter only sees decisions they are
    # entitled to read (approve/reject notes — e.g. why it was rejected), not
    # internal escalation notes.
    action_stmt = (
        select(CaseAction)
        .where(CaseAction.case_id == case.id, CaseAction.company_id == case.company_id)
        .order_by(CaseAction.created_at)
    )
    if current_user.role == UserRole.user:
        action_stmt = action_stmt.where(
            CaseAction.action_type.in_([CaseActionType.approve, CaseActionType.reject])
        )
    action_rows = db.execute(action_stmt).scalars().all()
    actor_ids = {a.actor_user_id for a in action_rows if a.actor_user_id}
    actors: dict[uuid.UUID, User] = {}
    if actor_ids:
        actors = {u.id: u for u in db.execute(select(User).where(User.id.in_(actor_ids))).scalars().all()}

    def _actor_role(a: CaseAction) -> str | None:
        # Role at action time; rows from before `actor_role` existed fall
        # back to the actor's current role.
        actor = actors.get(a.actor_user_id)
        return role_label(a.actor_role or (actor.role if actor else None)) or None

    actions = [
        CaseActionSchema(
            id=a.id,
            action_type=a.action_type.value,
            actor_name=(actors[a.actor_user_id].full_name or actors[a.actor_user_id].email)
            if a.actor_user_id in actors else None,
            actor_role=_actor_role(a),
            notes=a.notes,
            created_at=a.created_at,
        )
        for a in action_rows
    ]

    return CaseDetail(
        id=case.id,
        case_number=case.case_number,
        case_type=case.case_type,
        status=case.status,
        assigned_tier=case.assigned_tier,
        risk_tier=case.risk_tier if current_user.role != UserRole.user else None,
        submitted_by=UserSummary.model_validate(case.submitted_by) if case.submitted_by else None,
        created_at=case.created_at,
        document_count=len(documents),
        flag=_visible_flag(
            current_user, CaseFlagSchema.from_case_flag(get_case_flag(db, case.company_id, case.id))
        ),
        can_act=can_act_on_case(current_user, case),
        documents=documents,
        cross_document_findings=cross_document_findings,
        forensic_findings=forensic_findings,
        assessment=(
            RiskAssessmentSchema.from_assessment(
                assessment,
                {str(d.id): {c.check_type: c.summary for c in d.checks if c.summary} for d in documents},
            )
            if assessment and current_user.role != UserRole.user
            else None
        ),
        pipeline=PipelineStatusSchema(complete=pipeline.complete, pending=pipeline.pending),
        actions=actions,
    )


@router.get(
    "/{case_id}/audit-log",
    response_model=list[AuditLogEntrySchema],
    summary="Get a case's activity timeline",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "No such case (or, for a `user`, not one they submitted)."},
    },
)
def get_case_audit_log(
    case_id: uuid.UUID,
    scope: CaseScope = Depends(get_case_scope),
) -> list[AuditLogEntrySchema]:
    """Chronological feed for the case detail page's activity timeline —
    System Specification section 3.5. Reads the same append-only `audit_log` rows
    every check task and case/document endpoint already writes; this
    adds no new events, just a way to read the ones that exist."""
    db, current_user = scope.db, scope.ctx.user
    _load_visible_case(db, case_id, current_user)
    rows = db.execute(
        select(AuditLog)
        .where(
            AuditLog.case_id == case_id,
            AuditLog.company_id == scope.company_id,
            # Platform-only rows (company_id NULL) never appear here anyway;
            # explicit for defense in depth.
            AuditLog.event_type != "platform_admin_access",
        )
        .order_by(AuditLog.created_at.asc())
    ).scalars().all()

    actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
    actors: dict[uuid.UUID, User] = {}
    if actor_ids:
        actor_rows = db.execute(select(User).where(User.id.in_(actor_ids))).scalars().all()
        actors = {u.id: u for u in actor_rows}

    # Submitters are blinded to the risk signals (same rule as GET /cases/{id},
    # which withholds the assessment and tier): their timeline drops the
    # scoring event (score, tier, names of the rules that fired) and strips
    # the result details from the automated check events, keeping only the
    # fact that each step happened.
    is_reviewer = scope.ctx.is_platform_admin or has_rank(current_user.role, UserRole.reviewer_l1)
    if not is_reviewer:
        rows = [row for row in rows if row.event_type not in _SUBMITTER_HIDDEN_EVENTS]

    def _event_data(row: AuditLog) -> dict | None:
        if is_reviewer or not row.event_data:
            return row.event_data
        return {k: v for k, v in row.event_data.items() if k not in _SUBMITTER_HIDDEN_KEYS}

    return [
        AuditLogEntrySchema(
            id=row.id,
            event_type=row.event_type,
            actor_name=(
                (actors[row.actor_user_id].full_name or actors[row.actor_user_id].email)
                if row.actor_user_id in actors
                # Platform admins are not visible to a company session; their
                # support-access rows carry the admin's identity themselves.
                else (row.event_data or {}).get("admin_name") or (row.event_data or {}).get("admin_email")
            ),
            document_id=row.document_id,
            event_data=_event_data(row),
            created_at=row.created_at,
        )
        for row in rows
    ]
