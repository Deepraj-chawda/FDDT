"""
Risk scoring engine (SPECIFICATION.md section 3.3) — a transparent, weighted
RULES engine over the checks that have already run. Not a model.

Config-driven: every flaggable condition is a `risk_rules` row (see
app/models/risk_rule.py and app/services/risk_rule_seed.py). The `condition`
JSON on a rule says WHAT to match in the check results; `weight`,
`severity`, `reason_template` and `is_active` say what firing it means.
The generic matcher below reads those — no per-rule Python — so an admin
re-tunes scoring from Settings > Risk Rules without a redeploy.

Scoring a case:
  1. Only once its pipeline is complete (`pipeline_status`) — scoring a
     half-checked case would under-report and could let it be approved.
  2. Gather every completed check across the case's documents, the
     case-level cross-document findings, and any signature comparisons.
  3. Evaluate every current rule (highest version per rule_id). A rule
     fires once per document it matches (case-level rules once per case).
  4. Sum fired weights -> raw score; clamp to 0-100; bucket into a tier
     via the admin-editable thresholds in `risk_settings`.
  5. INSERT a `case_risk_assessments` row (never overwrite) whose snapshot
     names the immutable rule VERSIONS used, so later tuning can never
     retroactively change what a case was scored as.

Tenancy: rules and thresholds are per company. Every function here takes the
company explicitly and filters by it (on top of the session's own tenant
binding, app/db/tenancy.py) — a case is only ever scored against its own
company's rules.

Re-scoring: `request_case_scoring` is called as each check finishes. A run
whose *evidence* is unchanged from the latest assessment is skipped
(`evidence_fingerprint`), so a stray re-trigger can't silently re-score an
old case under newly tuned weights.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.cross_document_finding import CrossDocumentFinding
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.risk_rule import RiskRule
from app.models.risk_setting import (
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_METADATA_SCORE_CAP,
    DEFAULT_MEDIUM_THRESHOLD,
    RiskSetting,
)
from app.models.signature_match import SignatureMatch
from app.models.signature_reference import SignatureReference
from app.services.audit_service import record_event

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
_MATERIAL_SEVERITIES = ("medium", "high")

# Check types whose `details` is a list of findings — used to warn about a
# material finding no rule covers (a new check finding added without a rule).
_FINDING_CHECK_TYPES = {
    "metadata_forensics",
    "error_level_analysis",
    "copy_move_detection",
    "duplicate_detection",
    "visual_inconsistency_review",
    "font_consistency",
}

# Findings that are deliberately never scored: they are notes DERIVED from
# other checks (already scored on their own), so scoring them would
# double-count — e.g. the visual review's cross-reference to the metadata
# result — or explanatory notices. Kept out of the "no rule covers this"
# warning too, so that warning stays meaningful.
_UNSCORED_FINDINGS = {"metadata_forensics_correlation", "experimental_signal_notice"}

# Forensic checks every PDF must have a (completed OR failed) row for before
# its case is considered fully analysed. signature_stamp_detection is
# deliberately NOT required: it postdates older cases (which would then never
# score), and its own completion re-triggers scoring anyway.
_REQUIRED_PDF_CHECKS = (
    DocumentCheckType.metadata_forensics,
    DocumentCheckType.error_level_analysis,
    DocumentCheckType.copy_move_detection,
    DocumentCheckType.duplicate_detection,
    DocumentCheckType.visual_inconsistency_review,
)
_TERMINAL_CHECK_STATUSES = (DocumentCheckStatus.completed, DocumentCheckStatus.failed)
_TERMINAL_DOC_STATUSES = (DocumentProcessingStatus.complete, DocumentProcessingStatus.failed)


# ---------------------------------------------------------------------------
# Rules + thresholds
# ---------------------------------------------------------------------------

def load_current_rules(
    db: Session, company_id: uuid.UUID, *, include_inactive: bool = False
) -> list[RiskRule]:
    """The current (highest-version) row of every rule_id of one company."""
    latest = (
        select(RiskRule.rule_id.label("rule_id"), func.max(RiskRule.version).label("version"))
        .where(RiskRule.company_id == company_id)
        .group_by(RiskRule.rule_id)
        .subquery()
    )
    stmt = select(RiskRule).where(RiskRule.company_id == company_id).join(
        latest, and_(RiskRule.rule_id == latest.c.rule_id, RiskRule.version == latest.c.version)
    )
    if not include_inactive:
        stmt = stmt.where(RiskRule.is_active.is_(True))
    return list(db.execute(stmt.order_by(RiskRule.category, RiskRule.rule_id)).scalars().all())


def get_risk_settings(db: Session, company_id: uuid.UUID) -> RiskSetting:
    row = db.execute(
        select(RiskSetting).where(RiskSetting.company_id == company_id).order_by(RiskSetting.created_at)
    ).scalars().first()
    if row is None:
        row = RiskSetting(
            company_id=company_id,
            medium_threshold=DEFAULT_MEDIUM_THRESHOLD,
            high_threshold=DEFAULT_HIGH_THRESHOLD,
            metadata_score_cap=DEFAULT_METADATA_SCORE_CAP,
        )
        db.add(row)
        db.flush()
    return row


# Rule families whose points are capped together (risk_settings), by rule_id
# prefix: several rules there can fire from one underlying edit.
METADATA_RULE_PREFIX = "metadata."


def capped_raw_score(fired: list["FiredRule"], metadata_cap: float) -> tuple[float, dict[str, Any]]:
    """Sum of the fired rules' weights, with the metadata rules together
    counting at most `metadata_cap`. Returns (raw score, the caps applied as
    {"metadata": {"cap", "points"}} — empty when no cap bit)."""
    metadata = sum(f.rule.weight for f in fired if f.rule.rule_id.startswith(METADATA_RULE_PREFIX))
    other = sum(f.rule.weight for f in fired if not f.rule.rule_id.startswith(METADATA_RULE_PREFIX))
    if metadata <= metadata_cap:
        return float(metadata + other), {}
    return float(metadata_cap + other), {"metadata": {"cap": metadata_cap, "points": metadata}}


def tier_for_score(score: int, medium_threshold: int, high_threshold: int) -> RiskTier:
    if score >= high_threshold:
        return RiskTier.high
    if score >= medium_threshold:
        return RiskTier.medium
    return RiskTier.low


# ---------------------------------------------------------------------------
# Pipeline completeness
# ---------------------------------------------------------------------------

@dataclass
class PipelineStatus:
    complete: bool
    pending: list[str] = field(default_factory=list)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_pdf_document(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


def pipeline_status(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> PipelineStatus:
    """Whether every automated check for this case has finished, and if not,
    what is still outstanding (human-readable, shown to the reviewer)."""
    documents = db.execute(
        select(Document).where(Document.case_id == case_id, Document.company_id == company_id)
    ).scalars().all()
    if not documents:
        return PipelineStatus(False, ["No documents have been uploaded yet"])

    checks = db.execute(
        select(DocumentCheck.document_id, DocumentCheck.check_type, DocumentCheck.status).where(
            DocumentCheck.document_id.in_([d.id for d in documents]),
            DocumentCheck.company_id == company_id,
        )
    ).all()
    done: dict[uuid.UUID, set[DocumentCheckType]] = {}
    for document_id, check_type, status in checks:
        if status in _TERMINAL_CHECK_STATUSES:
            done.setdefault(document_id, set()).add(check_type)

    pending: list[str] = []
    for doc in documents:
        name = doc.original_filename
        if doc.processing_status not in _TERMINAL_DOC_STATUSES:
            pending.append(f"Text extraction and classification of '{name}'")
            continue  # its downstream checks can't be judged yet
        finished = done.get(doc.id, set())
        if doc.processing_status == DocumentProcessingStatus.complete:
            for check_type in (DocumentCheckType.field_validation, DocumentCheckType.issuer_verification):
                if check_type not in finished:
                    pending.append(f"{check_type.value.replace('_', ' ').capitalize()} of '{name}'")
        if is_pdf_document(doc):
            for check_type in _REQUIRED_PDF_CHECKS:
                if check_type not in finished:
                    pending.append(f"{check_type.value.replace('_', ' ').capitalize()} of '{name}'")

    if len(documents) >= 2 and not pending:
        latest_event = db.execute(
            select(func.max(AuditLog.created_at)).where(
                AuditLog.case_id == case_id,
                AuditLog.company_id == company_id,
                AuditLog.event_type == "cross_document_check_completed",
            )
        ).scalar_one_or_none()
        newest_upload = max(_aware(d.created_at) for d in documents)
        if latest_event is None or _aware(latest_event) < newest_upload:
            pending.append("Cross-document consistency check")

    return PipelineStatus(complete=not pending, pending=pending)


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

@dataclass
class _Evidence:
    documents: list[Document]
    # The completed check per (document, check_type) — exactly one row each
    # (unique constraint; re-runs overwrite it).
    checks: dict[tuple[uuid.UUID, str], DocumentCheck]
    cross_findings: list[CrossDocumentFinding]
    signature_matches: list[tuple[SignatureMatch, str]]  # (match, reference person_name)


def _gather_evidence(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> _Evidence:
    documents = list(
        db.execute(
            select(Document)
            .where(Document.case_id == case_id, Document.company_id == company_id)
            .order_by(Document.created_at)
        )
        .scalars()
        .all()
    )
    checks: dict[tuple[uuid.UUID, str], DocumentCheck] = {}
    rows = db.execute(
        select(DocumentCheck)
        .where(
            DocumentCheck.document_id.in_([d.id for d in documents]),
            DocumentCheck.company_id == company_id,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().all()
    for check in rows:
        checks[(check.document_id, check.check_type.value)] = check

    cross = list(
        db.execute(
            select(CrossDocumentFinding).where(
                CrossDocumentFinding.case_id == case_id, CrossDocumentFinding.company_id == company_id
            )
        )
        .scalars()
        .all()
    )
    sig_rows = db.execute(
        select(SignatureMatch, SignatureReference.person_name)
        .join(SignatureReference, SignatureReference.id == SignatureMatch.signature_reference_id)
        .where(SignatureMatch.case_id == case_id, SignatureMatch.company_id == company_id)
    ).all()
    return _Evidence(documents, checks, cross, [(m, name) for m, name in sig_rows])


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

@dataclass
class _Hit:
    """One rule matched once — on one document, or (document_id None) the case."""

    document_id: uuid.UUID | None
    document_filename: str | None
    count: int
    context: dict[str, Any]
    item_keys: list[tuple]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render_reason(template: str, context: dict[str, Any]) -> str:
    try:
        return template.format_map(_SafeDict(context)).strip()
    except (ValueError, IndexError, KeyError, AttributeError):
        return template


def _severity_ok(condition: dict[str, Any], severity: str | None) -> bool:
    allowed = condition.get("severity_in")
    return allowed is None or severity in allowed


def _finding_name_ok(condition: dict[str, Any], name: str | None) -> bool:
    if "finding" in condition:
        return name == condition["finding"]
    if "finding_in" in condition:
        return name in condition["finding_in"]
    return True


def _hits_for_rule(rule: RiskRule, ev: _Evidence) -> list[_Hit]:
    cond = rule.condition or {}
    kind = cond.get("match")
    hits: list[_Hit] = []

    if kind in ("finding", "sub_check", "check_result"):
        for doc in ev.documents:
            check = ev.checks.get((doc.id, cond.get("check_type", "")))
            if check is None or not isinstance(check.result, dict):
                continue
            result = check.result
            details = result.get("details")
            keys: list[tuple] = []
            ctx: dict[str, Any] = {"document": doc.original_filename}

            if kind == "finding":
                if not isinstance(details, list):
                    continue
                matched = []
                for idx, item in enumerate(details):
                    if not isinstance(item, dict):
                        continue
                    if _finding_name_ok(cond, item.get("finding")) and _severity_ok(cond, item.get("severity")):
                        matched.append(item)
                        keys.append((str(check.id), idx))
                if not matched:
                    continue
                first = matched[0]
                ctx.update(
                    count=len(matched),
                    page=first.get("page", ""),
                    description=first.get("description", ""),
                )
                if isinstance(first.get("data"), dict):
                    for k, v in first["data"].items():
                        ctx.setdefault(k, v)
                count = len(matched)

            elif kind == "sub_check":
                sub = details.get(cond.get("sub_check")) if isinstance(details, dict) else None
                if not isinstance(sub, dict) or sub.get("status") != "flag":
                    continue
                keys.append((str(check.id), cond["sub_check"]))
                ctx.update(reason=sub.get("reason", ""), count=1)
                count = 1

            else:  # check_result
                if result.get("result") != cond.get("result"):
                    continue
                keys.append((str(check.id), "result"))
                d = details if isinstance(details, dict) else {}
                ctx.update(issuer=d.get("issuer_name") or "(none extracted)", count=1)
                count = 1

            hits.append(_Hit(doc.id, doc.original_filename, count, ctx, keys))

    elif kind == "cross_document":
        matched_f = [
            f
            for f in ev.cross_findings
            if f.field_name == cond.get("field_name")
            and _severity_ok(cond, f.severity.value if hasattr(f.severity, "value") else str(f.severity))
        ]
        if matched_f:
            hits.append(
                _Hit(
                    None,
                    None,
                    len(matched_f),
                    {
                        "count": len(matched_f),
                        "field": cond.get("field_name"),
                        "description": matched_f[0].description,
                    },
                    [("cross", str(f.id)) for f in matched_f],
                )
            )

    elif kind == "signature_match":
        by_doc: dict[uuid.UUID, list[tuple[SignatureMatch, str]]] = {}
        for match, person in ev.signature_matches:
            value = match.result.value if hasattr(match.result, "value") else str(match.result)
            if value == cond.get("result"):
                by_doc.setdefault(match.document_id, []).append((match, person))
        docs_by_id = {d.id: d for d in ev.documents}
        for document_id, items in by_doc.items():
            doc = docs_by_id.get(document_id)
            if doc is None:
                continue
            match, person = items[0]
            hits.append(
                _Hit(
                    doc.id,
                    doc.original_filename,
                    len(items),
                    {
                        "document": doc.original_filename,
                        "person": person,
                        "reason": match.reasoning or "",
                        "count": len(items),
                    },
                    [("sig", str(m.id)) for m, _ in items],
                )
            )
    else:
        logger.warning("Risk rule %s has an unrecognised condition %r; skipping", rule.rule_id, cond)

    return hits


@dataclass
class FiredRule:
    rule: RiskRule
    hit: _Hit
    reason: str


def evaluate_rules(
    rules: list[RiskRule], ev: _Evidence
) -> tuple[list[FiredRule], list[str]]:
    """Returns (fired, warnings). Inactive rules still MATCH (so the
    unmatched-finding warning below doesn't cry wolf about a rule an admin
    deliberately switched off) but never fire."""
    fired: list[FiredRule] = []
    matched_keys: set[tuple] = set()
    for rule in rules:
        for hit in _hits_for_rule(rule, ev):
            matched_keys.update(hit.item_keys)
            if not rule.is_active:
                continue
            fired.append(FiredRule(rule, hit, render_reason(rule.reason_template, hit.context)))

    warnings: list[str] = []
    for (doc_id, check_type), check in ev.checks.items():
        if check_type not in _FINDING_CHECK_TYPES or not isinstance(check.result, dict):
            continue
        details = check.result.get("details")
        if not isinstance(details, list):
            continue
        for idx, item in enumerate(details):
            if (
                isinstance(item, dict)
                and item.get("severity") in _MATERIAL_SEVERITIES
                and item.get("finding") not in _UNSCORED_FINDINGS
                and (str(check.id), idx) not in matched_keys
            ):
                warnings.append(
                    f"No risk rule covers {check_type} finding {item.get('finding')!r} "
                    f"(severity {item.get('severity')}) — it was not scored."
                )
    for w in sorted(set(warnings)):
        logger.warning(w)
    return fired, warnings


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _fingerprint(ev: _Evidence, fired: list[FiredRule]) -> str:
    payload = {
        "documents": sorted(str(d.id) for d in ev.documents),
        "fired": sorted(
            (f.rule.rule_id, str(f.hit.document_id), f.hit.count) for f in fired
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def latest_assessment(
    db: Session, company_id: uuid.UUID, case_id: uuid.UUID
) -> CaseRiskAssessment | None:
    return db.execute(
        select(CaseRiskAssessment)
        .where(CaseRiskAssessment.case_id == case_id, CaseRiskAssessment.company_id == company_id)
        .order_by(CaseRiskAssessment.computed_at.desc())
    ).scalars().first()


def score_case(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> CaseRiskAssessment | None:
    """Score `case_id` (of `company_id`, against that company's own rules) if
    its pipeline is complete. Returns the (new or unchanged-latest)
    assessment, or None if the case can't be scored yet. The caller commits."""
    case = db.execute(
        select(Case).where(Case.id == case_id, Case.company_id == company_id)
    ).scalar_one_or_none()
    if case is None:
        return None
    if not pipeline_status(db, company_id, case_id).complete:
        return None

    ev = _gather_evidence(db, company_id, case_id)
    fired, _warnings = evaluate_rules(load_current_rules(db, company_id, include_inactive=True), ev)
    fingerprint = _fingerprint(ev, fired)

    latest = latest_assessment(db, company_id, case_id)
    if latest is not None and latest.evidence_fingerprint == fingerprint:
        return latest

    settings = get_risk_settings(db, company_id)
    fired.sort(key=lambda f: (-f.rule.weight, f.rule.rule_id))
    raw_score, group_caps = capped_raw_score(fired, settings.metadata_score_cap)
    score = max(0, min(100, round(raw_score)))
    tier = tier_for_score(score, settings.medium_threshold, settings.high_threshold)

    reasons = [
        {
            "rule_id": f.rule.rule_id,
            "rule_version": f.rule.version,
            "category": f.rule.category,
            "check_type": f.rule.check_type,
            "severity": f.rule.severity,
            "weight": f.rule.weight,
            "reason": f.reason,
            "document_id": str(f.hit.document_id) if f.hit.document_id else None,
            "document_filename": f.hit.document_filename,
        }
        for f in fired
    ]
    snapshot = {
        "rules": [
            {
                "rule_pk": str(f.rule.id),
                "rule_id": f.rule.rule_id,
                "version": f.rule.version,
                "weight": f.rule.weight,
                "severity": f.rule.severity,
            }
            for f in fired
        ],
        "thresholds": {"medium": settings.medium_threshold, "high": settings.high_threshold},
        "metadata_score_cap": settings.metadata_score_cap,
        "group_caps": group_caps,
    }

    assessment = CaseRiskAssessment(
        company_id=company_id,
        case_id=case_id,
        raw_score=raw_score,
        score=score,
        tier=tier,
        triggered_reasons=reasons,
        risk_rules_version_snapshot=snapshot,
        evidence_fingerprint=fingerprint,
    )
    # Unique (case_id, evidence_fingerprint): if a concurrent or redelivered
    # score_case already stored this exact evidence, keep that one.
    try:
        with db.begin_nested():
            db.add(assessment)
            db.flush()
    except IntegrityError:
        return db.execute(
            select(CaseRiskAssessment).where(
                CaseRiskAssessment.case_id == case_id,
                CaseRiskAssessment.company_id == company_id,
                CaseRiskAssessment.evidence_fingerprint == fingerprint,
            )
        ).scalar_one()
    case.risk_tier = tier
    db.flush()

    record_event(
        db,
        "risk_assessment_completed",
        case_id=case_id,
        event_data={
            "assessment_id": str(assessment.id),
            "score": score,
            "raw_score": raw_score,
            "tier": tier.value,
            "rules_fired": [r["rule_id"] for r in reasons],
        },
    )

    # First complete scoring hands the case to the reviewer queue.
    if case.status in (CaseStatus.submitted, CaseStatus.under_automated_review):
        from app.services import workflow_service  # local: workflow imports audit only

        workflow_service.mark_ready_for_review(db, case)

    return assessment


def request_case_scoring(case_id: uuid.UUID | str, company_id: uuid.UUID | str) -> None:
    """Queue a scoring attempt for a case. Called as each pipeline task
    finishes; harmless if the case isn't complete yet (score_case no-ops).
    A broker outage must never break the caller's own task."""
    try:
        from app.tasks.risk_scoring_task import score_case_task

        score_case_task.delay(str(case_id), str(company_id))
    except Exception:  # noqa: BLE001
        logger.warning("Could not enqueue risk scoring for case %s", case_id, exc_info=True)
