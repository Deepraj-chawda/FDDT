"""
Tenant context for Celery tasks.

Every pipeline task is enqueued with the company the document/case belongs to
(`task.delay(document_id, company_id)`) and opens a session bound to that
company, so worker code is confined by the same two layers as API requests:
the application-layer filter and PostgreSQL Row-Level Security.

A message enqueued before multi-tenancy carries no company; for those the
company is looked up once through the platform session (`resolve_*`) and the
task then proceeds company-bound as usual.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.session import system_session
from app.models.case import Case
from app.models.document import Document
from app.models.signature_reference import SignatureReference


def open_task_session(session_factory, company_id):
    """A pipeline task's session: bound to the company, and keeping loaded
    attributes after a commit (expire_on_commit=False).

    Tasks commit to END their read transaction before every slow step (blob
    download, Azure OCR/LLM call, CPU forensics). With the default
    expire-on-commit, merely reading `document.blob_storage_path` afterwards
    would silently open a new transaction and hold it — and its PgBouncer
    server connection — for the whole slow call. Holding no transaction during
    external calls is what lets transaction pooling share a small set of
    server connections among many workers."""
    from app.db import tenancy

    session = tenancy.bind_company(session_factory(), company_id)
    session.expire_on_commit = False
    return session


def _lookup(stmt) -> uuid.UUID | None:
    with system_session() as db:
        return db.execute(stmt).scalar_one_or_none()


def resolve_company_for_document(document_id: str, company_id: str | None) -> uuid.UUID | None:
    if company_id:
        return uuid.UUID(str(company_id))
    return _lookup(select(Document.company_id).where(Document.id == uuid.UUID(document_id)))


def resolve_company_for_case(case_id: str, company_id: str | None) -> uuid.UUID | None:
    if company_id:
        return uuid.UUID(str(company_id))
    return _lookup(select(Case.company_id).where(Case.id == uuid.UUID(case_id)))


def resolve_company_for_signature_reference(reference_id: str, company_id: str | None) -> uuid.UUID | None:
    if company_id:
        return uuid.UUID(str(company_id))
    return _lookup(
        select(SignatureReference.company_id).where(SignatureReference.id == uuid.UUID(reference_id))
    )
