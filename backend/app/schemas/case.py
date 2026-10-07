import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.case import CaseStatus, CaseTier, CaseType, RiskTier
from app.schemas.document import CaseDocumentSummary
from app.services.case_flag_service import CaseFlag, CaseFlagType, ForensicFinding
from app.services.check_summaries import risk_reason_short
from app.services.field_exception_service import cross_document_regions


class CaseCreateRequest(BaseModel):
    case_type: CaseType = Field(
        description="Case-level category chosen at submission. Independent of each document's own "
        "`document_type`, which the classification step fills in later."
    )


class CaseResponse(BaseModel):
    id: uuid.UUID
    case_number: str
    case_type: CaseType
    status: CaseStatus
    assigned_tier: CaseTier
    risk_tier: RiskTier | None
    submitted_by_user_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class UserSummary(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None

    model_config = {"from_attributes": True}


class CaseFlagSchema(BaseModel):
    """Wire shape of app/services/case_flag_service.py's CaseFlag: the
    case's real risk tier (low/medium/high), or "pending" until scored."""

    flag: CaseFlagType
    label: str
    description: str
    score: int | None = None

    @classmethod
    def from_case_flag(cls, case_flag: CaseFlag) -> "CaseFlagSchema":
        return cls(
            flag=case_flag.flag,
            label=case_flag.label,
            description=case_flag.description,
            score=case_flag.score,
        )


class CaseListItem(BaseModel):
    id: uuid.UUID
    case_number: str
    case_type: CaseType
    status: CaseStatus
    assigned_tier: CaseTier
    risk_tier: RiskTier | None
    submitted_by: UserSummary | None = None
    created_at: datetime
    document_count: int
    flag: CaseFlagSchema
    can_act: bool = Field(
        description="Whether the caller may approve/reject/escalate this case given its tier. "
        "False for submitters, and for a `reviewer_l1` on an L2-assigned case (read-only). "
        "Does not reflect status — a decided case is still `can_act` but every action is a 409."
    )


class CrossDocumentFindingSummary(BaseModel):
    """One `cross_document_findings` row (System Specification section 3.1,
    "Cross-document validation & reconciliation") — a shared field
    (amount/date/issuer) that disagreed between two of this case's
    documents. Case-level, not per-document — see app/services/
    cross_document_service.py and app/tasks/document_checks.py."""

    id: uuid.UUID
    field_name: str
    finding_type: str
    severity: str
    description: str
    document_ids: list[str] | None
    created_at: datetime
    # One highlight region per involved document whose field location is
    # known (app/services/field_exception_service.py) — drawn as a field-
    # exception box on each document, each captioned with what the other
    # showed. Empty when the fields weren't located.
    regions: list[dict[str, Any]] = []

    model_config = {"from_attributes": True}

    @classmethod
    def from_finding(cls, finding, documents_by_id: dict[str, Any] | None = None) -> "CrossDocumentFindingSummary":
        return cls(
            id=finding.id,
            field_name=finding.field_name,
            finding_type=finding.finding_type,
            severity=finding.severity.value,
            description=finding.description,
            document_ids=finding.document_ids,
            created_at=finding.created_at,
            regions=cross_document_regions(finding.field_name, finding.document_ids, documents_by_id or {}),
        )


class ForensicFindingSummary(BaseModel):
    """One medium/high-severity finding from a flagged metadata_forensics/
    error_level_analysis/copy_move_detection check — the per-document
    counterpart to CrossDocumentFindingSummary, both shown together in
    the case detail "Explainable findings" panel. See app/services/
    case_flag_service.py's get_forensic_findings for what feeds this and
    why it's a temporary stand-in for the real risk-scoring engine."""

    document_id: uuid.UUID
    document_filename: str
    check_type: str
    check_type_label: str
    finding: str
    severity: str
    description: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_finding(cls, finding: ForensicFinding) -> "ForensicFindingSummary":
        return cls(
            document_id=finding.document_id,
            document_filename=finding.document_filename,
            check_type=finding.check_type,
            check_type_label=finding.check_type_label,
            finding=finding.finding,
            severity=finding.severity,
            description=finding.description,
        )


class RiskReasonSchema(BaseModel):
    """One fired rule, as frozen at scoring time (rendered text, weight and
    rule version) — see app/models/case_risk_assessment.py."""

    rule_id: str
    rule_version: int
    category: str
    check_type: str
    severity: str
    weight: float
    reason: str
    document_id: uuid.UUID | None
    document_filename: str | None
    # Short form (app/services/check_summaries.py), computed when read.
    title: str = ""
    short: str = ""


class RiskAssessmentSchema(BaseModel):
    id: uuid.UUID
    score: int
    raw_score: float
    tier: RiskTier
    computed_at: datetime
    triggered_reasons: list[RiskReasonSchema]
    thresholds: dict[str, int]
    # Rule families whose points were capped together: {"metadata": {"cap", "points"}}.
    group_caps: dict[str, dict[str, float]] = {}

    @classmethod
    def from_assessment(cls, a, summaries: dict[str, dict[str, Any]] | None = None) -> "RiskAssessmentSchema":
        """`summaries`: {document_id: {check_type: check summary}} of the
        case's documents, for each reason's short line."""
        snapshot = a.risk_rules_version_snapshot or {}

        def reason(r: dict) -> RiskReasonSchema:
            title, short = risk_reason_short(r, (summaries or {}).get(str(r.get("document_id"))))
            return RiskReasonSchema(**{**r, "title": title, "short": short})

        return cls(
            id=a.id,
            score=a.score,
            raw_score=a.raw_score,
            tier=a.tier,
            computed_at=a.computed_at,
            triggered_reasons=[reason(r) for r in (a.triggered_reasons or [])],
            thresholds=snapshot.get("thresholds", {}),
            group_caps=snapshot.get("group_caps") or {},
        )


class PipelineStatusSchema(BaseModel):
    """Whether every automated check has finished. `pending` names what is
    still outstanding — shown to the reviewer when Approve is disabled."""

    complete: bool
    pending: list[str]


class CaseActionSchema(BaseModel):
    """A reviewer decision/note on the case (approve/reject/escalate)."""

    id: uuid.UUID
    action_type: str
    actor_name: str | None
    actor_role: str | None = Field(
        default=None, description="Display label of the actor's role when they acted, e.g. 'Reviewer L1'."
    )
    notes: str | None
    created_at: datetime


class CaseDetail(CaseListItem):
    documents: list[CaseDocumentSummary]
    cross_document_findings: list[CrossDocumentFindingSummary]
    forensic_findings: list[ForensicFindingSummary]
    assessment: RiskAssessmentSchema | None
    pipeline: PipelineStatusSchema
    actions: list[CaseActionSchema]


class ApproveRequest(BaseModel):
    note: str | None = Field(
        default=None,
        max_length=4000,
        description="Justification. Optional for low-risk cases; at least 10 characters (after "
        "trimming) is required to approve a medium- or high-risk case.",
    )


class RejectRequest(BaseModel):
    reason: str = Field(
        ..., max_length=4000, description="Why the case is rejected. Required and must not be blank."
    )

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("A reason is required to reject a case.")
        return v


class EscalateRequest(BaseModel):
    reason: str = Field(
        ..., max_length=4000, description="Why the case needs an L2 reviewer. Required and must not be blank."
    )

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("A reason is required to escalate a case.")
        return v


class CaseDecisionResponse(BaseModel):
    case_id: uuid.UUID
    status: CaseStatus
    assigned_tier: CaseTier
    action: CaseActionSchema


class AuditLogEntrySchema(BaseModel):
    """One `audit_log` row scoped to a case — System Specification section 3.5's
    "review history/timeline ... built from the same audit log." Every
    event_type here is a real one already written by app/services/
    audit_service.py's call sites (case creation, uploads, and each
    Celery check task) — nothing here is synthesized for display."""

    id: uuid.UUID
    event_type: str
    actor_name: str | None
    document_id: uuid.UUID | None
    event_data: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}
