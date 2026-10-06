"""
`issuer_fee_benchmarks` table — published fees of a known issuer (a school's
fee for a grade and academic year, per charge item), used by the fee
benchmark check (app/services/fee_benchmark_service.py) to spot one charge
on a document that is far above what the issuer actually charges.

Per company, like the issuer registry it hangs off. `verified` says whether
the figure was confirmed with the issuer or regulator (e.g. ADEK's approved
fee list) or only taken from a third-party listing; findings say which.

`item` is a normalized charge key: "tuition", "books", "uniform",
"transport", "technology", "registration", or "annual_total" for a single
all-in yearly fee.
"""
import uuid

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class IssuerFeeBenchmark(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "issuer_fee_benchmarks"

    issuer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("issuer_registry.id"), nullable=False, index=True
    )
    # "2023-24": the academic year's first calendar year is what is compared.
    academic_year: Mapped[str] = mapped_column(String(16), nullable=False)
    # Normalized grade ("KG2", "GRADE 5"), or "*" when the fee applies to every grade.
    grade: Mapped[str] = mapped_column(String(32), nullable=False)
    item: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    issuer = relationship("IssuerRegistry")
