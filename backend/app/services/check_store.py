"""
Idempotent writes of `document_checks` rows.

There is exactly ONE row per (document, check_type) — enforced by the
`uq_document_checks_document_check_type` unique constraint. Celery runs with
`task_acks_late`, so a worker that dies after committing but before
acknowledging gets its task re-delivered; the re-run must overwrite the row it
(or a failed earlier attempt) wrote, not add a second one. `save_check` is a
single atomic `INSERT ... ON CONFLICT (document_id, check_type) DO UPDATE`, so
concurrent or repeated runs of the same check always converge on one row
holding the most recent outcome. The full history of every run stays in the
append-only audit_log.

`company_id` is written explicitly (this is a Core insert, so the session's
ORM stamping doesn't apply) and must equal the session's company — the
PostgreSQL RLS policy's WITH CHECK rejects anything else.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType


def _insert(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def sanitize_null_bytes(val: Any) -> Any:
    if isinstance(val, str):
        return val.replace("\x00", "")
    if isinstance(val, dict):
        return {sanitize_null_bytes(k): sanitize_null_bytes(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_null_bytes(x) for x in val]
    return val


def save_check(
    db: Session,
    *,
    document: Document,
    check_type: DocumentCheckType,
    status: DocumentCheckStatus,
    result: dict[str, Any] | None = None,
    error_message: str | None = None,
    confidence: float | None = None,
) -> None:
    """Create or replace the single row for (document, check_type)."""
    table = DocumentCheck.__table__
    now = utcnow()
    clean_result = sanitize_null_bytes(result) if result is not None else None
    clean_error = error_message.replace("\x00", "") if error_message is not None else None
    stmt = _insert(db)(table).values(
        id=uuid.uuid4(),
        company_id=document.company_id,
        document_id=document.id,
        check_type=check_type,
        status=status,
        result=clean_result,
        error_message=clean_error,
        confidence=confidence,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.document_id, table.c.check_type],
        set_={
            "status": stmt.excluded.status,
            "result": stmt.excluded.result,
            "error_message": stmt.excluded.error_message,
            "confidence": stmt.excluded.confidence,
            "updated_at": now,
        },
    )
    db.execute(stmt)
