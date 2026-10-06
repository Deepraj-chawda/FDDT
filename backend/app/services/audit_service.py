"""
Append-only audit log writer (SPECIFICATION.md section 3.5/4).

Every automated check result and every human action is INSERTed via this
one function — nothing else in the codebase should UPDATE or DELETE an
`audit_log` row. This will also be where the workflow engine's emitted
internal events (see app/services/workflow_service.py) get persisted
once that's built.
"""
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record_event(
    db: Session,
    event_type: str,
    *,
    case_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    event_data: dict[str, Any] | None = None,
    company_id: uuid.UUID | None = None,
) -> AuditLog:
    """Append one audit row. `company_id` may be omitted on a company-bound
    session (it is stamped with the session's company, app/db/tenancy.py);
    on the platform session it is the company the event concerns, or None
    for a platform-level event (company created, user created, ...)."""
    entry = AuditLog(
        company_id=company_id,
        event_type=event_type,
        case_id=case_id,
        document_id=document_id,
        actor_user_id=actor_user_id,
        event_data=event_data,
    )
    db.add(entry)
    return entry
