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

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


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
