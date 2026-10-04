"""
`company_usage_stats` table — incrementally maintained usage counters for the
platform admin's billing & usage dashboard.

One row per (company, UTC day). The counters are bumped in the same
transaction that creates the thing being counted (case creation, document
upload, report generation — see app/services/usage_service.py), so the
dashboard reads a handful of small rows instead of running SUM/COUNT over the
cases/documents tables on every load. Any date range (this month, all time,
...) is a sum over the days in it.

Daily rows rather than one running total per company, because billing is
periodic: a single all-time counter could not answer "this month".

`reconcile_usage_stats` (a nightly Celery beat job) recomputes every row from
the source tables and corrects any drift, so the counters are fast but the
source data stays the source of truth. Nothing is ever deleted from cases/
documents/reports today, so the counters only ever go up; if deletion is added
it must decrement them (or rely on the nightly reconciliation).

Storage counts the bytes of original documents and generated report PDFs
(the two kinds of file the platform stores per case, both with their exact
size recorded at write time). Signature crops are small derived images and
are not counted.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class CompanyUsageStats(Base):
    __tablename__ = "company_usage_stats"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("companies.id"), primary_key=True
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)

    cases_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    documents_uploaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # documents + generated reports
    files_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
