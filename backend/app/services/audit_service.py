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


def record_platform_event(
    system_db: Session,
    event_type: str,
    *,
    admin,
    company=None,
    company_id: uuid.UUID | None = None,
    case_id: uuid.UUID | None = None,
    event_data: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one PLATFORM-ONLY audit row for something a platform admin did
    (created a user, edited a company's risk rules...). Written through the
    platform session with company_id NULL, so no company session can ever
    read it — Row-Level Security hides NULL-company rows from the app role.
    The company it concerns goes in event_data.company_id, which is how a
    platform admin's view of that company's log finds it (app/api/audit.py).
    `company` (a Company) or `company_id` names it; neither = platform-level."""
    target_id = company.id if company is not None else company_id
    data = {
        **(event_data or {}),
        "platform_admin": True,
        "admin_email": admin.email,
        "admin_name": admin.full_name,
        "company_id": str(target_id) if target_id is not None else None,
    }
    if company is not None:
        data["company_name"] = company.name
    return record_event(
        system_db,
        event_type,
        case_id=case_id,
        actor_user_id=admin.id,
        event_data=data,
        company_id=None,  # platform-only: invisible to every company session
    )
