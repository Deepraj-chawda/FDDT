"""initial schema

Revision ID: e7ce49024f9e
Revises:
Create Date: 2026-09-12 03:18:29.116136

Creates the core v1 tables from SPECIFICATION.md section 6:
users, cases, documents, document_checks, cross_document_findings,
risk_scores, risk_rules, case_actions, audit_log.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7ce49024f9e"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column(
            "role",
            sa.Enum("user", "reviewer", "admin", name="user_role"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("case_number", sa.String(64), nullable=False),
        sa.Column(
            "submitted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "submitted",
                "under_automated_review",
                "pending_manual_review",
                "auto_approved",
                "escalated",
                "under_investigation",
                "approved",
                "rejected",
                "closed",
                name="case_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "risk_tier", sa.Enum("low", "medium", "high", name="risk_tier"), nullable=True
        ),
        sa.Column("external_ref_id", sa.String(255), nullable=True),
        sa.UniqueConstraint("case_number"),
    )
    op.create_index("ix_cases_case_number", "cases", ["case_number"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False
        ),
        sa.Column(
            "uploaded_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("blob_storage_path", sa.String(1024), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("document_type", sa.String(128), nullable=True),
    )
    op.create_index("ix_documents_case_id", "documents", ["case_id"])
    op.create_index("ix_documents_file_hash", "documents", ["file_hash"])

    op.create_table(
        "document_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id"),
            nullable=False,
        ),
        sa.Column(
            "check_type",
            sa.Enum(
                "ocr_layout",
                "classification",
                "field_extraction",
                "issuer_validation",
                "signature_stamp_detection",
                "metadata_forensics",
                "error_level_analysis",
                "copy_move_detection",
                "duplicate_detection",
                "ai_content_detection",
                name="document_check_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "completed", "failed", name="document_check_status"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("error_message", sa.String(2048), nullable=True),
    )
    op.create_index("ix_document_checks_document_id", "document_checks", ["document_id"])

    op.create_table(
        "cross_document_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False
        ),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("finding_type", sa.String(128), nullable=False),
        sa.Column(
            "severity",
            sa.Enum("info", "low", "medium", "high", name="finding_severity"),
            nullable=False,
        ),
        sa.Column("description", sa.String(2048), nullable=False),
        sa.Column("document_ids", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_cross_document_findings_case_id", "cross_document_findings", ["case_id"]
    )

    op.create_table(
        "risk_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("condition", postgresql.JSONB(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("reason_template", sa.String(1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("rule_id"),
    )
    op.create_index("ix_risk_rules_rule_id", "risk_rules", ["rule_id"])

    op.create_table(
        "risk_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column(
            "tier", sa.Enum("low", "medium", "high", name="risk_score_tier"), nullable=False
        ),
        sa.Column("triggered_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("rules_snapshot", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_risk_scores_case_id", "risk_scores", ["case_id"])

    op.create_table(
        "case_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False
        ),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "action_type",
            sa.Enum(
                "assign",
                "comment",
                "approve",
                "reject",
                "escalate",
                "reopen",
                name="case_action_type",
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.String(4096), nullable=True),
    )
    op.create_index("ix_case_actions_case_id", "case_actions", ["case_id"])

    # audit_log is append-only at the application layer (SPECIFICATION.md section
    # 3.5/4): no UPDATE/DELETE path is exposed anywhere in the API for it.
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=True
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_data", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_case_id", "audit_log", ["case_id"])
    op.create_index("ix_audit_log_document_id", "audit_log", ["document_id"])
    op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("case_actions")
    op.drop_table("risk_scores")
    op.drop_table("risk_rules")
    op.drop_table("cross_document_findings")
    op.drop_table("document_checks")
    op.drop_table("documents")
    op.drop_table("cases")
    op.drop_table("users")

    # Drop enum types explicitly — Postgres does not drop them automatically
    # when the last table referencing them is dropped.
    for enum_name in (
        "case_action_type",
        "risk_score_tier",
        "finding_severity",
        "document_check_status",
        "document_check_type",
        "risk_tier",
        "case_status",
        "user_role",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
