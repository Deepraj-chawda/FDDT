"""
`issuer_registry` table — known vendors, schools, and other document
issuers an extracted issuer name is fuzzy-matched against (SPECIFICATION.md
section 3.1, "Issuer validation"; see app/services/issuer_service.py).

NOTE: seeded (see the Alembic migration that creates this table) with a
small set of FAKE test entries for local development/pipeline testing
only. This table must be populated with the client's real vendor/school/
tax-ID data before the issuer verification check means anything in
production.
"""
import enum

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class IssuerType(str, enum.Enum):
    vendor = "vendor"
    school = "school"
    government = "government"
    other = "other"


class IssuerRegistry(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "issuer_registry"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Optional Arabic-script name for the same real-world issuer — a
    # single English `name` can't be fuzzy-matched against an Arabic
    # extracted issuer name (different scripts, not just spelling
    # variance; see app/services/issuer_service.py). Nullable: not every
    # issuer has a known Arabic name on file.
    name_arabic: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ISO 3166-1 alpha-2 country the issuer operates in ("AE"), when known.
    # Decides whether the registry has entries relevant to a document at all
    # (app/services/issuer_service.py): with none, an unmatched issuer is
    # "not checked" rather than "not in registry".
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    type: Mapped[IssuerType] = mapped_column(
        Enum(IssuerType, name="issuer_registry_type"), default=IssuerType.other, nullable=False
    )
    # Soft delete: issuers are never hard-deleted (historical cases and
    # issuer_verification results refer to them by name). An inactive
    # issuer is ignored by the issuer-verification match
    # (app/services/issuer_service.py) but stays listed for admins.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
