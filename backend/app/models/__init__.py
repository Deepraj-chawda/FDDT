"""
Import every model module here so `Base.metadata` is fully populated for
Alembic autogenerate and for `Base.metadata.create_all()` in tests.
"""
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.bulk_upload import BulkUpload, BulkUploadCase, BulkUploadStatus
from app.models.case import Case, CaseTier, CaseStatus, CaseType, RiskTier
from app.models.case_action import CaseAction, CaseActionType
from app.models.case_report import CaseReport
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.company import Company, TenantScopedMixin
from app.models.company_usage_stats import CompanyUsageStats
from app.models.cross_document_finding import CrossDocumentFinding, FindingSeverity
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.document_page_hash import DocumentPageHash
from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.models.risk_rule import RiskRule
from app.models.risk_rule_template import RiskRuleTemplate
from app.models.risk_score import RiskScore
from app.models.risk_setting import RiskSetting
from app.models.signature_match import ComparisonScope, SignatureMatch, SignatureMatchResult
from app.models.signature_reference import SignatureReference
from app.models.user import User, UserRole

__all__ = [
    "Base",
    "BulkUpload",
    "BulkUploadCase",
    "BulkUploadStatus",
    "Company",
    "CompanyUsageStats",
    "TenantScopedMixin",
    "User",
    "UserRole",
    "Case",
    "CaseStatus",
    "CaseType",
    "RiskTier",
    "Document",
    "DocumentCheck",
    "DocumentCheckType",
    "DocumentCheckStatus",
    "DocumentPageHash",
    "CrossDocumentFinding",
    "FindingSeverity",
    "IssuerRegistry",
    "IssuerType",
    "RiskRule",
    "RiskRuleTemplate",
    "RiskScore",
    "RiskSetting",
    "CaseRiskAssessment",
    "CaseReport",
    "CaseTier",
    "CaseAction",
    "CaseActionType",
    "AuditLog",
    "SignatureReference",
    "SignatureMatch",
    "ComparisonScope",
    "SignatureMatchResult",
]
