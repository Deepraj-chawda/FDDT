"""
`audit_log` table — the append-only system-of-record (SPECIFICATION.md
section 3.5). Every automated check result and every human action is
INSERTed here; the application layer must never UPDATE or DELETE rows in
this table (there is deliberately no update/delete path exposed anywhere
in the API for this model).

Rows are also how the workflow engine's emitted internal events
(`case_created`, `case_status_changed`, `case_escalated`, ...) are
persisted once app/services/workflow_service.py exists — this table is
one of the event subscribers described in SPECIFICATION.md section 4.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow
from app.models.company import TenantScopedMixin


class AuditLog(TenantScopedMixin, UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"
    # NULL for platform-level events (company created, user created, usage
    # reconciliation...) — those are invisible to every company.
    __tenant_nullable__ = True

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=True, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_data: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
