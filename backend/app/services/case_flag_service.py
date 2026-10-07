"""
Case-level flag — the single coloured pill per case shown on the case
queue/dashboard and the case-detail header.

The flag is the case's REAL risk tier, read from its latest
`case_risk_assessments` row (app/services/risk_scoring_service.py):
low / medium / high. A case that has not been scored yet (its automated
checks are still running) is "pending" rather than being given a guess.
This applies to single-document cases too — forensic checks run on that one
document, so it gets a real tier; only the cross-document comparison is
inapplicable to it, not the whole assessment.

`get_forensic_findings` (below) is unchanged: it lists the medium/high
findings of flagged per-document forensic checks for the case-detail
"Explainable findings" panel.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.document import Document
from app.models.document_check import DocumentCheckStatus, DocumentCheckType
from app.services.check_summaries import risk_reason_short

CaseFlagType = Literal["low", "medium", "high", "pending"]

_LABELS: dict[CaseFlagType, str] = {
    "low": "Low risk",
    "medium": "Medium risk",
    "high": "High risk",
    "pending": "Analyzing",
}

# The per-document forensic checks whose flagged findings are listed in the
# case-detail "Explainable findings" source data (see get_forensic_findings
# below) — deliberately NOT field_validation/issuer_verification, which are
# about data plausibility, not document integrity. duplicate_detection counts
# too: a near-identical resubmission is a document-integrity concern the same
# way tampering is.
_FORENSIC_CHECK_TYPES = (
    DocumentCheckType.metadata_forensics,
    DocumentCheckType.error_level_analysis,
    DocumentCheckType.copy_move_detection,
    DocumentCheckType.duplicate_detection,
)
_FORENSIC_CHECK_LABELS: dict[DocumentCheckType, str] = {
    DocumentCheckType.metadata_forensics: "Metadata forensics",
    DocumentCheckType.error_level_analysis: "Error level analysis",
    DocumentCheckType.copy_move_detection: "Copy-move forgery detection",
    DocumentCheckType.duplicate_detection: "Duplicate detection",
}
# Only these findings are returned by get_forensic_findings —
# the "info" severity findings each forensic check also writes (e.g.
# metadata_forensics recording the document ID chain "for future
# duplicate/lineage analysis") are data points, not something a reviewer
# needs surfaced as a reason this case is flagged.
_MATERIAL_FINDING_SEVERITIES = ("medium", "high")


@dataclass
class CaseFlag:
    flag: CaseFlagType
    label: str
    description: str
    # 0-100 risk score of the latest assessment; None while "pending".
    score: int | None = None


@dataclass
class ForensicFinding:
    """One medium/high-severity finding from a flagged forensic check,
    the document-level counterpart to CrossDocumentFinding — both are
    surfaced together in the case detail "Explainable findings" panel.
    Unlike a CrossDocumentFinding, this belongs to one specific document,
    not the case as a whole, hence document_id/document_filename."""

    document_id: uuid.UUID
    document_filename: str
    check_type: str
    check_type_label: str
    finding: str
    severity: str
    description: str


def _describe_assessment(reasons: list[dict[str, Any]]) -> str:
    if not reasons:
        return "No risk signals fired across this case's documents."
    # Stored highest-weight first; their short titles (app/services/check_summaries.py).
    def label(r: dict[str, Any]) -> str:
        title, short = risk_reason_short(r, None)
        return title if r.get("rule_id") else short

    titles = list(dict.fromkeys(label(r) for r in reasons))
    shown = " · ".join(titles[:3])
    extra = len(titles) - 3
    return shown if extra <= 0 else f"{shown} (+{extra} more)"


def _flag_from_assessment(assessment: CaseRiskAssessment | None) -> CaseFlag:
    if assessment is None:
        return CaseFlag(
            flag="pending",
            label=_LABELS["pending"],
            description="Automated checks are still running — a risk tier will appear once they finish.",
            score=None,
        )
    tier: CaseFlagType = assessment.tier.value  # type: ignore[assignment]
    return CaseFlag(
        flag=tier,
        label=_LABELS[tier],
        description=_describe_assessment(list(assessment.triggered_reasons or [])),
        score=assessment.score,
    )


def get_case_flags(
    db: Session, company_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, CaseFlag]:
    """Batched flag lookup — one query for every id in `case_ids` (the case
    queue list endpoint), picking each case's most recent assessment."""
    if not case_ids:
        return {}
    latest: dict[uuid.UUID, CaseRiskAssessment] = {}
    rows = db.execute(
        select(CaseRiskAssessment)
        .where(CaseRiskAssessment.case_id.in_(case_ids), CaseRiskAssessment.company_id == company_id)
        .order_by(CaseRiskAssessment.computed_at)
    ).scalars().all()
    for row in rows:  # ascending, so the last one per case wins
        latest[row.case_id] = row
    return {case_id: _flag_from_assessment(latest.get(case_id)) for case_id in case_ids}


def get_case_flag(db: Session, company_id: uuid.UUID, case_id: uuid.UUID) -> CaseFlag:
    return get_case_flags(db, company_id, [case_id])[case_id]


def get_forensic_findings(documents: Sequence[Document]) -> list[ForensicFinding]:
    """Flattens every medium/high-severity finding from a flagged
    metadata_forensics/error_level_analysis/copy_move_detection/
    duplicate_detection check, across every document already loaded onto
    a case (expects each
    Document's `.checks` relationship to be eagerly loaded — see
    app/api/cases.py's get_case_detail), into one case-level list. This
    is the forensic-checks half of the case detail "Explainable
    findings" panel; app/schemas/case.py's cross_document_findings is
    the other half."""
    out: list[ForensicFinding] = []
    for doc in documents:
        for check in doc.checks:
            if check.check_type not in _FORENSIC_CHECK_TYPES:
                continue
            if check.status != DocumentCheckStatus.completed:
                continue
            result: Any = check.result or {}
            if not isinstance(result, dict) or result.get("result") != "flag":
                continue
            details = result.get("details")
            if not isinstance(details, list):
                continue
            for finding in details:
                if not isinstance(finding, dict):
                    continue
                severity = finding.get("severity")
                if severity not in _MATERIAL_FINDING_SEVERITIES:
                    continue
                out.append(
                    ForensicFinding(
                        document_id=doc.id,
                        document_filename=doc.original_filename,
                        check_type=check.check_type.value,
                        check_type_label=_FORENSIC_CHECK_LABELS.get(
                            check.check_type, check.check_type.value
                        ),
                        finding=str(finding.get("finding", "")),
                        severity=str(severity),
                        description=str(finding.get("description", "")),
                    )
                )
    return out
