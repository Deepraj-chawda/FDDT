"""
`users` table — email/password auth with a `role` field.

Two kinds of account:

* Company users — `user` (0) < `reviewer_l1` (1) < `reviewer_l2` (2). Each
  belongs to exactly one company (`company_id` NOT NULL for them) and only
  ever sees that company's data. Each role can do everything the roles below
  it can, so permission checks gate on a minimum rank (`has_rank`, and
  `require_company_role` in app/api/auth.py) rather than on lists of role
  names. `reviewer_l2` additionally manages its own company's issuer registry
  and risk rules.

* `platform_admin` — the platform operator's own team. Not tied to any
  company (`company_id` IS NULL). Creates companies and every user account,
  sees the cross-company billing/usage dashboard, and has audited read-only
  support access to any company's cases. It is deliberately NOT part of the
  company rank ladder: it cannot approve/reject/escalate or upload, so it is
  never "a reviewer plus more" (see `is_platform_admin`).

`reviewer_l1` is the former `reviewer` role. The former single-tenant `admin`
role was migrated to `platform_admin`.
"""
import enum

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class UserRole(str, enum.Enum):
    user = "user"
    reviewer_l1 = "reviewer_l1"
    reviewer_l2 = "reviewer_l2"
    platform_admin = "platform_admin"


# Rank of the company roles. platform_admin is intentionally absent: it sits
# outside every company and is checked with `is_platform_admin`, never by rank.
ROLE_RANK: dict[UserRole, int] = {
    UserRole.user: 0,
    UserRole.reviewer_l1: 1,
    UserRole.reviewer_l2: 2,
}

COMPANY_ROLES = tuple(ROLE_RANK)

ROLE_LABELS: dict[UserRole, str] = {
    UserRole.user: "User",
    UserRole.reviewer_l1: "Reviewer L1",
    UserRole.reviewer_l2: "Reviewer L2",
    UserRole.platform_admin: "Platform Admin",
}


def is_platform_admin(role: UserRole) -> bool:
    return role == UserRole.platform_admin


def has_rank(role: UserRole, minimum: UserRole) -> bool:
    """True if `role` is a company role at least as privileged as `minimum`.
    Always False for platform_admin — it has no company rank."""
    if role not in ROLE_RANK or minimum not in ROLE_RANK:
        return False
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


def role_label(role: UserRole | str | None) -> str:
    """Display label ("Reviewer L1"); tolerates a raw string from stored
    event data, including the pre-rename "reviewer" and the pre-multi-tenancy
    "admin"."""
    if role is None:
        return ""
    try:
        return ROLE_LABELS[UserRole(role)]
    except ValueError:
        return {"reviewer": "Reviewer L1", "admin": "Admin"}.get(str(role), str(role))


class User(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    # NULL only for platform_admin accounts (enforced by a CHECK constraint
    # in the migration and by the user-management API).
    __tenant_nullable__ = True

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.user, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    submitted_cases = relationship(
        "Case", back_populates="submitted_by", foreign_keys="Case.submitted_by_user_id"
    )
    company = relationship("Company")

    @property
    def is_platform_admin(self) -> bool:
        return self.role == UserRole.platform_admin
