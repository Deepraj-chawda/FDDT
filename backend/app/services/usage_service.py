"""
Per-company usage counters for the platform admin's billing & usage dashboard
(see app/models/company_usage_stats.py for the table and why it is per day).

`record_*` are called in the SAME transaction as the thing they count, so a
rolled-back upload never inflates the counters. Each is a single atomic
`INSERT ... ON CONFLICT DO UPDATE SET n = n + :delta`, so concurrent uploads
from many workers/requests never lose an increment.

`usage_by_company` is what the dashboard reads: a sum over at most one small
row per company per day — never a COUNT/SUM over cases or documents.

`reconcile_usage` recomputes every (company, day) row from the source tables
and corrects drift. It runs nightly (Celery beat, app/tasks/usage_tasks.py) as
the correctness backstop and can be run on demand from the dashboard.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.bulk_upload import BulkUpload
from app.models.case import Case
from app.models.case_report import CaseReport
from app.models.company import Company
from app.models.company_usage_stats import CompanyUsageStats
from app.models.document import Document

_COUNTERS = ("cases_created", "documents_uploaded", "files_stored", "storage_bytes")


def _upsert_increment(db: Session, company_id: uuid.UUID, day: date, **deltas: int) -> None:
    deltas = {k: int(v) for k, v in deltas.items() if v}
    if not deltas:
        return
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    table = CompanyUsageStats.__table__
    stmt = insert(table).values(
        company_id=company_id,
        usage_date=day,
        updated_at=utcnow(),
        **{name: deltas.get(name, 0) for name in _COUNTERS},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.company_id, table.c.usage_date],
        set_={
            **{name: table.c[name] + delta for name, delta in deltas.items()},
            "updated_at": utcnow(),
        },
    )
    db.execute(stmt)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def record_case_created(db: Session, company_id: uuid.UUID) -> None:
    _upsert_increment(db, company_id, _today(), cases_created=1)


def record_document_uploaded(db: Session, company_id: uuid.UUID, size_bytes: int) -> None:
    _upsert_increment(
        db, company_id, _today(), documents_uploaded=1, files_stored=1, storage_bytes=size_bytes
    )


def record_report_stored(db: Session, company_id: uuid.UUID, size_bytes: int) -> None:
    _upsert_increment(db, company_id, _today(), files_stored=1, storage_bytes=size_bytes)


def record_bulk_zip_stored(db: Session, company_id: uuid.UUID, size_bytes: int) -> None:
    """The submitted zip itself (kept in Blob Storage as the upload's record).
    Its documents are counted one by one as they are ingested."""
    _upsert_increment(db, company_id, _today(), files_stored=1, storage_bytes=size_bytes)


# ---------------------------------------------------------------------------
# Dashboard read
# ---------------------------------------------------------------------------


@dataclass
class CompanyUsage:
    company_id: uuid.UUID
    company_name: str
    is_active: bool
    cases_created: int
    documents_uploaded: int
    files_stored: int
    storage_bytes: int
    # Current totals, regardless of the selected period — storage is a stock,
    # not just a flow, and billing usually needs both.
    total_storage_bytes: int
    total_files_stored: int


def usage_by_company(
    db: Session, *, start: date | None = None, end: date | None = None
) -> list[CompanyUsage]:
    """Per-company usage for [start, end] (inclusive, UTC days; None = open).
    Platform session only."""
    in_period = select(
        CompanyUsageStats.company_id,
        func.coalesce(func.sum(CompanyUsageStats.cases_created), 0),
        func.coalesce(func.sum(CompanyUsageStats.documents_uploaded), 0),
        func.coalesce(func.sum(CompanyUsageStats.files_stored), 0),
        func.coalesce(func.sum(CompanyUsageStats.storage_bytes), 0),
    ).group_by(CompanyUsageStats.company_id)
    if start is not None:
        in_period = in_period.where(CompanyUsageStats.usage_date >= start)
    if end is not None:
        in_period = in_period.where(CompanyUsageStats.usage_date <= end)
    period = {row[0]: row[1:] for row in db.execute(in_period).all()}

    totals = {
        row[0]: row[1:]
        for row in db.execute(
            select(
                CompanyUsageStats.company_id,
                func.coalesce(func.sum(CompanyUsageStats.storage_bytes), 0),
                func.coalesce(func.sum(CompanyUsageStats.files_stored), 0),
            ).group_by(CompanyUsageStats.company_id)
        ).all()
    }

    result = []
    for company in db.execute(select(Company).order_by(Company.name)).scalars().all():
        cases, docs, files, storage = period.get(company.id, (0, 0, 0, 0))
        total_storage, total_files = totals.get(company.id, (0, 0))
        result.append(
            CompanyUsage(
                company_id=company.id,
                company_name=company.name,
                is_active=company.is_active,
                cases_created=int(cases),
                documents_uploaded=int(docs),
                files_stored=int(files),
                storage_bytes=int(storage),
                total_storage_bytes=int(total_storage),
                total_files_stored=int(total_files),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Reconciliation (nightly correctness backstop)
# ---------------------------------------------------------------------------


def _day_expr(db: Session):
    """UTC calendar day of a timestamptz column — the same day boundary the
    counters use, whatever the database session's TimeZone is."""
    if db.get_bind().dialect.name == "postgresql":
        return lambda column: cast(func.timezone("UTC", column), Date)
    return lambda column: func.date(column)  # SQLite (tests): 'YYYY-MM-DD'


def compute_true_usage(db: Session) -> dict[tuple[uuid.UUID, date], dict[str, int]]:
    """Recompute every (company, day) counter from the source tables.
    Platform session only (it reads every company)."""
    true: dict[tuple[uuid.UUID, date], dict[str, int]] = defaultdict(lambda: dict.fromkeys(_COUNTERS, 0))
    _day = _day_expr(db)

    def _key(company_id, day):
        if isinstance(day, str):  # SQLite returns CAST(... AS DATE) as text
            day = date.fromisoformat(day[:10])
        return company_id, day

    for company_id, day, n in db.execute(
        select(Case.company_id, _day(Case.created_at), func.count()).group_by(
            Case.company_id, _day(Case.created_at)
        )
    ).all():
        true[_key(company_id, day)]["cases_created"] += int(n)

    for company_id, day, n, size in db.execute(
        select(
            Document.company_id,
            _day(Document.created_at),
            func.count(),
            func.coalesce(func.sum(Document.file_size_bytes), 0),
        ).group_by(Document.company_id, _day(Document.created_at))
    ).all():
        counters = true[_key(company_id, day)]
        counters["documents_uploaded"] += int(n)
        counters["files_stored"] += int(n)
        counters["storage_bytes"] += int(size)

    for company_id, day, n, size in db.execute(
        select(
            CaseReport.company_id,
            _day(CaseReport.generated_at),
            func.count(),
            func.coalesce(func.sum(CaseReport.file_size_bytes), 0),
        ).group_by(CaseReport.company_id, _day(CaseReport.generated_at))
    ).all():
        counters = true[_key(company_id, day)]
        counters["files_stored"] += int(n)
        counters["storage_bytes"] += int(size)

    for company_id, day, n, size in db.execute(
        select(
            BulkUpload.company_id,
            _day(BulkUpload.created_at),
            func.count(),
            func.coalesce(func.sum(BulkUpload.zip_size_bytes), 0),
        ).group_by(BulkUpload.company_id, _day(BulkUpload.created_at))
    ).all():
        counters = true[_key(company_id, day)]
        counters["files_stored"] += int(n)
        counters["storage_bytes"] += int(size)

    return dict(true)


@dataclass
class ReconciliationResult:
    rows_checked: int
    rows_corrected: int
    drift: list[dict]


def reconcile_usage(db: Session) -> ReconciliationResult:
    """Make company_usage_stats match the source data exactly. The caller
    commits. Returns what was corrected (for the log/audit trail)."""
    true = compute_true_usage(db)
    stored = {
        (row.company_id, row.usage_date): row
        for row in db.execute(select(CompanyUsageStats)).scalars().all()
    }
    drift: list[dict] = []
    for key in set(true) | set(stored):
        expected = true.get(key, dict.fromkeys(_COUNTERS, 0))
        row = stored.get(key)
        actual = {name: getattr(row, name) for name in _COUNTERS} if row else dict.fromkeys(_COUNTERS, 0)
        if actual == expected:
            continue
        drift.append(
            {
                "company_id": str(key[0]),
                "usage_date": key[1].isoformat(),
                "counters": {n: {"was": actual[n], "now": expected[n]} for n in _COUNTERS if actual[n] != expected[n]},
            }
        )
        if row is None:
            db.add(CompanyUsageStats(company_id=key[0], usage_date=key[1], **expected))
        else:
            for name, value in expected.items():
                setattr(row, name, value)
    db.flush()
    return ReconciliationResult(rows_checked=len(set(true) | set(stored)), rows_corrected=len(drift), drift=drift)
