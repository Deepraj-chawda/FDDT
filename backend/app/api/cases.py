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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, and_, case as sql_case, false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.auth import get_tenant_db, require_company_role, require_platform_admin
from app.api.case_access import can_act_on_case, load_visible_case
from app.api.tenant_access import CaseScope, CompanyScope, _load_company, company_scope, get_case_scope
from app.db import tenancy
from app.db.session import get_system_db
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.case import Case, CaseStatus, CaseTier, CaseType, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
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
    CaseListPage,
    CaseListSummary,
    CaseResponse,
    CaseRiskCounts,
    CrossDocumentFindingSummary,
    DeleteCaseRequest,
    ForensicFindingSummary,
    UserSummary,
)
from app.schemas.document import CaseDocumentSummary
from app.services.audit_service import record_event, record_platform_event
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


def _visible_flag(user: User, flag: CaseFlagSchema, case_status: CaseStatus | None = None) -> CaseFlagSchema:
    """Reviewers/admins get the real risk tier + score. A submitter does NOT:
    seeing which signals fired (and how heavily) on their own submission
    would tell a bad actor exactly what to evade. They only get a neutral
    marker — "In review" while the case is open, "Review complete" once a
    decision is in (never the tier); the reviewer's decision and reasons
    still reach them via the case's approve/reject actions."""
    if user.role != UserRole.user:  # reviewers and platform admins
        return flag
    if case_status in _DECIDED or case_status == CaseStatus.auto_approved:
        return CaseFlagSchema(
            flag="pending",
            label="Review complete",
            description="The review team has finished reviewing your case.",
            score=None,
        )
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


@router.post(
    "/{case_id}/discard",
    status_code=204,
    summary="Discard an empty case (its submitter)",
    description=(
        "Withdraws a case that never received a document — e.g. every file of a new case was "
        "rejected at upload — so it does not linger in the queue, the counts and the dashboard as "
        "an unanalysable case. Only the user who created the case may discard it, and only while "
        "it has no documents (409 otherwise). It is a soft delete, the same as a platform admin's "
        "`DELETE /cases/{case_id}` (the company no longer sees the case; a platform admin can "
        "still restore it), audited as `case_discarded`."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Platform admins have read-only access."},
        404: {"description": "No such case, or not one the caller created."},
        409: {"description": "The case already has documents."},
    },
)
def discard_empty_case(
    case_id: uuid.UUID,
    current_user: User = Depends(require_company_role(UserRole.user)),
    scope: CaseScope = Depends(get_case_scope),
) -> None:
    db = scope.db
    case = db.execute(
        select(Case).where(Case.id == case_id, Case.company_id == scope.company_id).with_for_update()
    ).scalar_one_or_none()
    # Someone else's case reads as missing, for reviewers too: only its creator withdraws it.
    if case is None or case.submitted_by_user_id != current_user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    if db.execute(select(func.count(Document.id)).where(Document.case_id == case.id)).scalar_one():
        raise HTTPException(status.HTTP_409_CONFLICT, "This case already has documents and can't be discarded.")
    case.deleted_at = utcnow()
    case.deleted_by_user_id = current_user.id
    record_event(
        db,
        "case_discarded",
        case_id=case.id,
        actor_user_id=current_user.id,
        event_data={"case_number": case.case_number, "reason": "No document was accepted."},
    )
    db.commit()


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
    current_user = scope.ctx.user
    stmt = _with_document_counts(_visible_cases(scope, actionable=actionable))
    stmt = _default_order(stmt, current_user)
    if tier_filter is not None:
        stmt = stmt.where(Case.assigned_tier == tier_filter)
    if status_filter is not None:
        stmt = stmt.where(Case.status == status_filter)
    if case_type_filter is not None:
        stmt = stmt.where(Case.case_type == case_type_filter)
    return _list_items(scope, scope.db.execute(stmt).all())


# --- Paginated list + counts (the case queue, dashboard and My Cases pages) ---

# The queue's status groups. A case is in exactly one of analyzing /
# awaiting_review / approved / rejected; the other groups overlap them.
_APPROVED = (CaseStatus.auto_approved, CaseStatus.approved, CaseStatus.closed)
_ANALYZING = (CaseStatus.submitted, CaseStatus.under_automated_review)
_CLOSED_OUT = (*_APPROVED, CaseStatus.rejected)

StatusGroup = Literal[
    "analyzing", "awaiting_review", "approved", "rejected", "open", "escalated", "deleted",
    "in_progress", "action_required", "pending_review",
]
RiskFilter = Literal["high", "medium", "low", "pending"]


def _visible_cases(scope: CompanyScope, *, actionable: bool = False, mine: bool = False) -> Select:
    """Every case the caller may list: their company's, and only their own
    for a submitter (or with `mine`). `actionable`: only the ones they may
    act on — none for submitters and platform admins, L1 cases for a
    Reviewer L1."""
    user = scope.ctx.user
    stmt = select(Case).where(Case.company_id == scope.company_id)
    if user.role == UserRole.user or mine:
        stmt = stmt.where(Case.submitted_by_user_id == user.id)
    if actionable:
        if user.role == UserRole.user or user.is_platform_admin:
            stmt = stmt.where(false())
        elif not has_rank(user.role, UserRole.reviewer_l2):
            stmt = stmt.where(Case.assigned_tier == CaseTier.l1)
    return stmt


def _with_document_counts(stmt: Select) -> Select:
    return (
        stmt.add_columns(func.count(Document.id))
        .outerjoin(Document, Document.case_id == Case.id)
        .options(selectinload(Case.submitted_by))
        .group_by(Case.id)
    )


def _default_order(stmt: Select, user: User) -> Select:
    if user.is_platform_admin or has_rank(user.role, UserRole.reviewer_l2):
        # Surfacing escalations is the point of the L2 tier: open L2 cases
        # sort to the top (a CASE expression, not an enum sort: enum
        # ordering differs by database).
        stmt = stmt.order_by(
            sql_case((and_(Case.assigned_tier == CaseTier.l2, Case.status.notin_(_DECIDED)), 0), else_=1)
        )
    return stmt.order_by(Case.created_at.desc())


def _status_group_clause(group: StatusGroup, user: User):
    escalated = and_(Case.assigned_tier == CaseTier.l2, Case.status.notin_(_CLOSED_OUT))
    # My Cases: a rejection, or (reviewers only — a submitter never learns
    # the risk tier) a high-risk case not yet cleared or under investigation.
    attention = Case.status == CaseStatus.rejected
    if user.role != UserRole.user:
        attention = or_(attention, Case.risk_tier == RiskTier.high)
    action_required = and_(Case.status.notin_((*_APPROVED, CaseStatus.under_investigation)), attention)
    return {
        "analyzing": Case.status.in_(_ANALYZING),
        "awaiting_review": Case.status.notin_((*_ANALYZING, *_CLOSED_OUT)),
        "approved": Case.status.in_(_APPROVED),
        "rejected": Case.status == CaseStatus.rejected,
        "open": Case.status.notin_(_CLOSED_OUT),
        "escalated": escalated,
        "in_progress": Case.status == CaseStatus.under_investigation,
        "action_required": action_required,
        "pending_review": and_(
            Case.status.notin_((*_APPROVED, CaseStatus.under_investigation)), ~action_required
        ),
    }[group]


def _risk_clause(risk: RiskFilter, user: User):
    if user.role == UserRole.user:
        # Every case reads "in review" to a submitter: filtering on the real
        # tier would disclose it.
        return Case.id.is_not(None) if risk == "pending" else false()
    return Case.risk_tier.is_(None) if risk == "pending" else Case.risk_tier == RiskTier(risk)


def _search_clause(q: str):
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    submitter = select(User.id).where(
        User.id == Case.submitted_by_user_id,
        or_(User.full_name.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\")),
    )
    return or_(Case.case_number.ilike(pattern, escape="\\"), submitter.exists())


def _latest_score():
    return (
        select(CaseRiskAssessment.score)
        .where(CaseRiskAssessment.case_id == Case.id)
        .order_by(CaseRiskAssessment.computed_at.desc())
        .limit(1)
        .correlate(Case)
        .scalar_subquery()
    )


_LIST_DOCUMENT_NAMES = 3


def _document_names(db: Session, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """The first few original filenames of each case, in upload order."""
    names: dict[uuid.UUID, list[str]] = {case_id: [] for case_id in case_ids}
    if not case_ids:
        return names
    ranked = (
        select(
            Document.case_id,
            Document.original_filename,
            func.row_number()
            .over(partition_by=Document.case_id, order_by=(Document.created_at, Document.id))
            .label("n"),
        )
        .where(Document.case_id.in_(case_ids))
        .subquery()
    )
    rows = db.execute(
        select(ranked.c.case_id, ranked.c.original_filename)
        .where(ranked.c.n <= _LIST_DOCUMENT_NAMES)
        .order_by(ranked.c.case_id, ranked.c.n)
    ).all()
    for case_id, filename in rows:
        names[case_id].append(filename)
    return names


def _list_items(scope: CompanyScope, rows) -> list[CaseListItem]:
    current_user = scope.ctx.user
    flags_by_case = get_case_flags(scope.db, scope.company_id, [case.id for case, _ in rows])
    names_by_case = _document_names(scope.db, [case.id for case, _ in rows])
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
            document_names=names_by_case[case.id],
            flag=_visible_flag(
                current_user, CaseFlagSchema.from_case_flag(flags_by_case[case.id]), case.status
            ),
            can_act=can_act_on_case(current_user, case),
            deleted_at=case.deleted_at,
        )
        for case, document_count in rows
    ]


@router.get(
    "/page",
    response_model=CaseListPage,
    summary="List cases, one page at a time",
    description=(
        "The same cases, visibility and order as `GET /cases`, filtered and paginated on the "
        "server. `status_group`: analyzing, awaiting_review, approved, rejected, open, escalated "
        "(open and assigned to L2), in_progress / action_required / pending_review (My Cases), "
        "or deleted (platform admins: their soft-deleted cases; every other group lists live "
        "cases only). `risk`: the latest risk tier, `pending` = not scored (for a submitter "
        "every case is `pending`). `q` matches the case number or the submitter's name or "
        "email. `mine=true`: only the caller's own submissions. `sort=priority`: open L2 "
        "cases first, then the highest latest risk score, then newest."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        400: {"description": "A platform admin did not pass `company_id`."},
    },
)
def list_cases_page(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=255),
    status_group: StatusGroup | None = Query(default=None),
    risk: RiskFilter | None = Query(default=None),
    case_type_filter: CaseType | None = Query(default=None, alias="case_type"),
    tier_filter: CaseTier | None = Query(default=None, alias="assigned_tier"),
    actionable: bool = Query(default=False),
    mine: bool = Query(default=False),
    sort: Literal["default", "priority"] = Query(default="default"),
    scope: CompanyScope = Depends(company_scope("case list")),
) -> CaseListPage:
    db, current_user = scope.db, scope.ctx.user
    stmt = _visible_cases(scope, actionable=actionable, mine=mine)
    if status_group == "deleted":
        stmt = stmt.where(Case.deleted_at.is_not(None))
    elif status_group is not None:
        stmt = stmt.where(Case.deleted_at.is_(None), _status_group_clause(status_group, current_user))
    if risk is not None:
        stmt = stmt.where(_risk_clause(risk, current_user))
    if q and q.strip():
        stmt = stmt.where(_search_clause(q.strip()))
    if case_type_filter is not None:
        stmt = stmt.where(Case.case_type == case_type_filter)
    if tier_filter is not None:
        stmt = stmt.where(Case.assigned_tier == tier_filter)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    page = _with_document_counts(stmt)
    if sort == "priority" and current_user.role != UserRole.user:
        page = page.order_by(
            sql_case((and_(Case.assigned_tier == CaseTier.l2, Case.status.notin_(_CLOSED_OUT)), 0), else_=1),
            func.coalesce(_latest_score(), -1).desc(),
            Case.created_at.desc(),
        )
    else:
        page = _default_order(page, current_user)
    rows = db.execute(page.order_by(Case.id).limit(limit).offset(offset)).all()
    return CaseListPage(items=_list_items(scope, rows), total=total, limit=limit, offset=offset)


@router.get(
    "/summary",
    response_model=CaseListSummary,
    summary="Case counts for the queue, dashboard and My Cases cards",
    description=(
        "Counts over every live case the caller can see (`mine=true`: only their own "
        "submissions), in the `status_group` terms of `GET /cases/page`. A submitter's risk "
        "counts are all `pending` and `high_risk_open` is 0: tiers are never disclosed to them."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        400: {"description": "A platform admin did not pass `company_id`."},
    },
)
def case_list_summary(
    mine: bool = Query(default=False),
    scope: CompanyScope = Depends(company_scope("case list")),
) -> CaseListSummary:
    db, user = scope.db, scope.ctx.user
    live = _visible_cases(scope, mine=mine).where(Case.deleted_at.is_(None))
    cases = live.subquery()
    is_submitter = user.role == UserRole.user

    def count(clause):
        return func.coalesce(func.sum(sql_case((clause, 1), else_=0)), 0)

    groups = (
        "analyzing", "awaiting_review", "approved", "rejected", "open", "escalated",
        "in_progress", "action_required",
    )
    actionable_ids = _visible_cases(scope, actionable=True, mine=mine).with_only_columns(Case.id)
    row = db.execute(
        select(
            func.count(),
            *(count(_status_group_clause(g, user)) for g in groups),
            count(and_(_status_group_clause("open", user), Case.risk_tier == RiskTier.high)),
            *(count(Case.risk_tier == tier) for tier in (RiskTier.high, RiskTier.medium, RiskTier.low)),
            count(Case.risk_tier.is_(None)),
            count(Case.id.in_(actionable_ids)),
            count(Case.assigned_tier == CaseTier.l2),
        ).select_from(Case).where(Case.id.in_(select(cases.c.id)))
    ).one()
    total, *group_counts, high_open, high, medium, low, unscored, actionable, l2 = (int(v) for v in row)
    by_group = dict(zip(groups, group_counts))
    documents = db.execute(
        select(func.count(Document.id)).where(Document.case_id.in_(select(cases.c.id)))
    ).scalar_one()
    risk = (
        CaseRiskCounts(pending=total)
        if is_submitter
        else CaseRiskCounts(high=high, medium=medium, low=low, pending=unscored)
    )
    return CaseListSummary(
        total=total,
        documents=int(documents),
        **by_group,
        high_risk_open=0 if is_submitter else high_open,
        risk=risk,
        actionable=actionable,
        l2=l2,
    )


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
        "read any company's case (including a deleted one); each such access is written to the "
        "platform-only audit log."
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
            current_user, CaseFlagSchema.from_case_flag(get_case_flag(db, case.company_id, case.id)), case.status
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
        deleted_at=case.deleted_at,
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
    system_db: Session = Depends(get_system_db),
) -> list[AuditLogEntrySchema]:
    """Chronological feed for the case detail page's activity timeline —
    System Specification section 3.5. Reads the same append-only `audit_log` rows
    every check task and case/document endpoint already writes; this
    adds no new events, just a way to read the ones that exist.

    Company users: the company's rows only — never a platform admin's.
    Platform admins: EVERYTHING about the case, including the platform-only
    rows (deleted / restored with the reason, every support view), read
    through the platform session."""
    db, current_user = scope.db, scope.ctx.user
    _load_visible_case(db, case_id, current_user)
    if scope.ctx.is_platform_admin:
        state = tenancy.snapshot(system_db)
        tenancy.bind_platform(system_db)
        try:
            rows = system_db.execute(
                select(AuditLog)
                .where(
                    AuditLog.case_id == case_id,
                    or_(AuditLog.company_id == scope.company_id, AuditLog.company_id.is_(None)),
                )
                .order_by(AuditLog.created_at.asc())
            ).scalars().all()
            actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
            actors: dict[uuid.UUID, User] = (
                {u.id: u for u in system_db.execute(select(User).where(User.id.in_(actor_ids))).scalars()}
                if actor_ids else {}
            )
        finally:
            tenancy.restore(system_db, state)
    else:
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
        actors = {}
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
        data = row.event_data
        if not data:
            return None
        if not scope.ctx.is_platform_admin:
            data = {k: v for k, v in data.items() if k not in ("admin_email", "admin_name")}
        if not is_reviewer:
            data = {k: v for k, v in data.items() if k not in _SUBMITTER_HIDDEN_KEYS}
        return data

    def _resolve_actor(row: AuditLog) -> str | None:
        if row.actor_user_id in actors:
            return actors[row.actor_user_id].full_name or actors[row.actor_user_id].email
        if not scope.ctx.is_platform_admin and (
            row.event_type in ("case_deleted", "case_restored")
            or (row.event_data or {}).get("platform_admin")
        ):
            return "Platform Administrator"
        return (row.event_data or {}).get("admin_name") or (row.event_data or {}).get("admin_email")

    return [
        AuditLogEntrySchema(
            id=row.id,
            event_type=row.event_type,
            actor_name=_resolve_actor(row),
            document_id=row.document_id,
            event_data=_event_data(row),
            created_at=row.created_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Delete / restore (platform admins only)
# ---------------------------------------------------------------------------

_DELETE_RESPONSES = {
    401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
    403: {"description": "Platform admins only."},
    404: {"description": "No such case."},
}


def _set_deleted(scope: CaseScope, admin: User, system_db: Session, *, deleted: bool, reason: str | None) -> None:
    """Soft delete or restore, and audit it in the company's log so company reviewers
    can see that it was deleted/restored by admin."""
    case = scope.db.get(Case, scope.case_id)
    if (case.deleted_at is not None) == deleted:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The case is already deleted." if deleted else "The case is not deleted."
        )
    case.deleted_at = utcnow() if deleted else None
    case.deleted_by_user_id = admin.id if deleted else None

    event_type = "case_deleted" if deleted else "case_restored"
    event_data = {
        "case_number": case.case_number,
        "reason": (reason or "").strip() or None,
        "platform_admin": True,
        "admin_email": admin.email,
        "admin_name": admin.full_name,
        "company_id": str(scope.company_id),
    }
    record_event(
        scope.db,
        event_type,
        case_id=case.id,
        actor_user_id=admin.id,
        event_data=event_data,
        company_id=scope.company_id,
    )
    scope.db.commit()


@router.delete(
    "/{case_id}",
    status_code=204,
    summary="Delete a case (platform admin only)",
    description=(
        "Soft delete: the case disappears for its company everywhere — case lists, the case page, "
        "bulk uploads, the audit log, duplicate and linked-case checks — as if it never existed. "
        "Its documents, results and files are kept, and `POST /cases/{case_id}/restore` brings it "
        "back. Recorded only in the platform audit log (`case_deleted`)."
    ),
    responses={**_DELETE_RESPONSES, 409: {"description": "The case is already deleted."}},
)
def delete_case(
    case_id: uuid.UUID,
    payload: DeleteCaseRequest | None = None,
    admin: User = Depends(require_platform_admin),
    scope: CaseScope = Depends(get_case_scope),
    system_db: Session = Depends(get_system_db),
) -> None:
    _set_deleted(scope, admin, system_db, deleted=True, reason=payload.reason if payload else None)


@router.post(
    "/{case_id}/restore",
    status_code=204,
    summary="Restore a deleted case (platform admin only)",
    description="Undoes `DELETE /cases/{case_id}`. Recorded only in the platform audit log (`case_restored`).",
    responses={**_DELETE_RESPONSES, 409: {"description": "The case is not deleted."}},
)
def restore_case(
    case_id: uuid.UUID,
    admin: User = Depends(require_platform_admin),
    scope: CaseScope = Depends(get_case_scope),
    system_db: Session = Depends(get_system_db),
) -> None:
    _set_deleted(scope, admin, system_db, deleted=False, reason=None)
