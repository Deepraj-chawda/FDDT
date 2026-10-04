"""
`companies` table — one row per client company (tenant).

Every tenant-owned table carries a `company_id` FK to this table (see
`TenantScopedMixin` below). Company users (`user`, `reviewer_l1`,
`reviewer_l2`) belong to exactly one company and only ever see that company's
rows; `platform_admin` users have no company and sit outside all of them.

Isolation is enforced twice (see docs/multi-tenancy.md):
  1. Application layer — every ORM query a tenant-bound session runs is
     filtered by `company_id` automatically (app/db/tenancy.py), on top of the
     explicit `company_id` arguments the services take.
  2. Database layer — PostgreSQL Row-Level Security policies on every
     tenant-owned table, keyed on the `app.current_company_id` setting the
     tenant session sets at the start of each transaction.

`is_active=False` suspends a company: its users can no longer sign in or use
an existing token. Its data is kept.
"""
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # Upload limits, a per-company capacity / plan-tier lever that only a
    # platform admin changes (app/api/platform.py). Stored on the row at
    # creation time from the then-current defaults, so changing the default
    # later never silently changes an existing company. Read through
    # app/services/upload_limits.py by every upload path.
    max_file_size_mb: Mapped[int] = mapped_column(
        Integer, nullable=False, default=lambda: _default_limits()[0]
    )
    max_zip_size_mb: Mapped[int] = mapped_column(
        Integer, nullable=False, default=lambda: _default_limits()[1]
    )

    __table_args__ = (
        CheckConstraint("max_file_size_mb > 0", name="ck_companies_max_file_size_mb_positive"),
        CheckConstraint("max_zip_size_mb > 0", name="ck_companies_max_zip_size_mb_positive"),
    )


def _default_limits() -> tuple[int, int]:
    from app.core.config import settings

    return settings.default_max_file_size_mb, settings.default_max_zip_size_mb


class TenantScopedMixin:
    """Adds the `company_id` column that ties a row to one company.

    Every model using this mixin is filtered by the session's company
    automatically (app/db/tenancy.py) and is covered by a Row-Level Security
    policy in the database. `__tenant_nullable__ = True` is only for tables
    that also hold platform-level rows (`users` for platform admins,
    `audit_log` for platform events) — a NULL `company_id` row is invisible to
    every company.
    """

    __tenant_nullable__ = False

    @declared_attr
    def company_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PG_UUID(as_uuid=True),
            ForeignKey("companies.id"),
            nullable=cls.__tenant_nullable__,
            index=True,
        )
