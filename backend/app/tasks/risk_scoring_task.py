"""
Celery task: score a case (app/services/risk_scoring_service.py).

Enqueued via `request_case_scoring` as each pipeline task finishes. It's a
no-op until every check for the case has completed, and again when the
evidence hasn't changed since the last assessment, so calling it liberally
is cheap and safe.
"""
import uuid

from app.db import tenancy
from app.db.session import SessionLocal
from app.services.risk_scoring_service import score_case
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_case


@celery_app.task(name="score_case")
def score_case_task(case_id: str, company_id: str | None = None) -> None:
    company_uuid = resolve_company_for_case(case_id, company_id)
    if company_uuid is None:
        return
    db = open_task_session(SessionLocal, company_uuid)
    try:
        score_case(db, company_uuid, uuid.UUID(case_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
