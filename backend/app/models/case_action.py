"""
`case_actions` table — every human reviewer/admin action taken on a case
(approve/reject/escalate/assign/comment). Distinct from `audit_log`: this
table is the reviewer-facing action history; `audit_log` is the complete
append-only system-of-record (automated checks + human actions + state
transitions).
"""
import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class CaseActionType(str, enum.Enum):
    assign = "assign"
    comment = "comment"
    approve = "approve"
    reject = "reject"
    escalate = "escalate"
    reopen = "reopen"


class CaseAction(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_actions"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    action_type: Mapped[CaseActionType] = mapped_column(
        Enum(CaseActionType, name="case_action_type"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # The actor's role AT THE TIME of the action (a `UserRole` value), so the
    # history still reads "escalated by Reviewer L1" after that user is
    # promoted. Null only for rows written before this column existed.
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    case = relationship("Case", back_populates="actions")
