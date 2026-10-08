"""
Test fixtures. Uses an in-memory SQLite DB (instead of Postgres) purely to
keep this foundation's test suite dependency-free — SQLite gets the
JSON/JSONB variant fallback declared on the relevant model columns.

Multi-tenancy: the suite runs inside a default test company. `db_session` is
bound to it (app/db/tenancy.py), so rows a test creates directly are stamped
with that company and reads are filtered to it — exactly like a company
request. Requests through `client` re-bind the same session per request (the
caller's company, or platform mode for platform-admin routes) and restore the
test's binding afterwards. PostgreSQL Row-Level Security itself is exercised
separately by tests/test_rls_postgres.py, which needs a real Postgres.
"""
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.auth import token_claims
from app.core.security import create_access_token, hash_password
from app.db import tenancy
from app.db.session import get_db, get_system_db
from app.main import app
from app.models import Base
from app.models.company import Company
from app.models.user import User, UserRole
from app.services.forensics.pdf_render import RenderedPage, render_pdf_pages
from app.services.storage_service import StorageService, get_storage_service
from app.tasks.bulk_upload_task import ingest_bulk_upload
from app.tasks.document_checks import run_cross_document_checks, run_document_checks
from app.tasks.document_processing import process_document
from app.tasks.duplicate_check_task import run_duplicate_check_task
from app.tasks.metadata_forensics_task import run_metadata_forensics
from app.tasks.signature_detection_task import run_signature_detection
from app.tasks.risk_scoring_task import score_case_task
from app.tasks.tampering_checks_task import run_tampering_checks
from app.tasks.visual_inconsistency_task import run_visual_inconsistency_review_task

# backend/tests/conftest.py -> backend -> project root -> sample-documents/
_SAMPLE_DOCUMENTS_DIR = Path(__file__).resolve().parents[2] / "sample-documents"


class FakeStorageService(StorageService):
    """In-memory stand-in for Azure Blob Storage, proving the
    StorageService abstraction actually decouples the upload endpoint
    from a specific backend — tests never touch real Azure."""

    def __init__(self):
        self.uploads: dict[str, bytes] = {}

    def upload(self, blob_path, content, content_type=None):
        if hasattr(content, "read"):  # a file object (bulk-upload zips)
            content = content.read()
        self.uploads[blob_path] = content
        return f"https://fake.blob.core.windows.net/documents/{blob_path}"

    def get_download_url(self, file_url, expires_in_minutes=15):
        return f"{file_url}?fake-sas-token"

    def download_bytes(self, file_url):
        return self.uploads[file_url.rsplit("/documents/", 1)[-1]]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite doesn't enforce foreign keys by default — turn it on so this
    # suite catches FK/flush-ordering bugs the way Postgres would, rather
    # than silently accepting a bad insert order.
    event.listen(
        engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON")
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    tenancy.bind_platform(session)
    company = Company(name="Test Company")
    session.add(company)
    session.commit()
    session.info["test_company_id"] = company.id
    tenancy.bind_company(session, company.id)
    try:
        yield session
    finally:
        session.close()


def tenant_task_factory(monkeypatch, engine):
    """Session factory for tests that call Celery task functions directly
    against their own engine: sessions come out bound to a fresh test
    company, and the tasks' company lookup for messages without one
    (app/tasks/tenant.py) uses the same engine in platform mode."""
    import app.tasks.tenant as task_tenant

    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    setup = maker()
    tenancy.bind_platform(setup)
    task_company = Company(name="Task Test Company")
    setup.add(task_company)
    setup.commit()
    company_id = task_company.id
    setup.close()

    def factory():
        return tenancy.bind_company(maker(), company_id)

    @contextmanager
    def _system_session():
        session = tenancy.bind_platform(maker())
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(task_tenant, "system_session", _system_session)
    factory.company_id = company_id
    return factory


@pytest.fixture()
def company(db_session):
    """The default company every fixture user and test row belongs to."""
    tenancy.bind_platform(db_session)
    try:
        return db_session.get(Company, db_session.info["test_company_id"])
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])


def make_company(db_session, name: str) -> Company:
    """Another company (a second tenant) for isolation tests."""
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        other = Company(name=name)
        db_session.add(other)
        db_session.commit()
        return other
    finally:
        tenancy.restore(db_session, state)


def set_upload_limits(db_session, company_id, *, file_mb: int | None = None, zip_mb: int | None = None) -> None:
    """Set a company's per-company upload limits directly (what a platform
    admin does through PATCH /platform/companies/{id})."""
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        company = db_session.get(Company, company_id)
        if file_mb is not None:
            company.max_file_size_mb = file_mb
        if zip_mb is not None:
            company.max_zip_size_mb = zip_mb
        db_session.commit()
    finally:
        tenancy.restore(db_session, state)


@pytest.fixture()
def fake_storage():
    return FakeStorageService()


@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    """Uploads enqueue `process_document.delay(...)` and
    `run_metadata_forensics.delay(...)` (see app/api/documents.py) —
    independent of each other — and process_document itself enqueues
    `run_document_checks.delay(...)` (see app/tasks/document_checks.py)
    on success, which in turn can enqueue
    `run_cross_document_checks.delay(...)`. Without a real Celery worker
    + Redis broker in the test run, any of these would either hang trying
    to reach Redis or (worse) quietly need one — and even if they ran,
    each task opens its own `SessionLocal()` against real Postgres and
    calls real Azure/LLM APIs, none of which belong in this unit-test
    suite. No-op all five here instead; pipeline behavior is covered
    separately (tests/test_document_processing.py, tests/test_document_
    checks.py, tests/test_metadata_forensics_task.py, tests/
    test_duplicate_check_task.py), not through the upload endpoint's
    tests."""
    monkeypatch.setattr(process_document, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_document_checks, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_cross_document_checks, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_metadata_forensics, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_tampering_checks, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_duplicate_check_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_signature_detection, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_visual_inconsistency_review_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(score_case_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(ingest_bulk_upload, "delay", lambda *args, **kwargs: None)


class FakeRedis:
    """The few Redis commands the sign-in throttle uses, in memory. TTLs are
    remembered, not enforced (tests read them back)."""

    def __init__(self):
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        value = self.values.get(key)
        return None if value is None else str(value).encode()

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.ttls[key] = int(seconds)
        return True

    def ttl(self, key):
        return self.ttls.get(key, -1) if key in self.values else -2

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.ttls.pop(key, None)


@pytest.fixture(autouse=True)
def login_redis(monkeypatch):
    """Sign-in throttling (app/services/login_throttle.py) runs against an
    in-memory Redis per test: no real broker needed, no counts leaking
    between tests that sign in with the same address."""
    from app.services import login_throttle

    fake = FakeRedis()
    monkeypatch.setattr(login_throttle, "_redis", lambda: fake)
    return fake


@pytest.fixture()
def client(db_session, fake_storage):
    # Production hands each request a fresh session; the suite shares one, so
    # each request's tenant binding is undone when the request ends.
    def _get_db_override():
        state = tenancy.snapshot(db_session)
        tenancy.restore(db_session, (None, None))
        try:
            yield db_session
        finally:
            tenancy.restore(db_session, state)

    def _get_system_db_override():
        state = tenancy.snapshot(db_session)
        tenancy.bind_platform(db_session)
        try:
            yield db_session
        finally:
            tenancy.restore(db_session, state)

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_system_db] = _get_system_db_override
    app.dependency_overrides[get_storage_service] = lambda: fake_storage
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_user(db_session):
    """The company's most privileged role (reviewer_l2: every reviewer action
    plus the company's issuer registry and risk rules). Platform-wide powers
    live on `platform_admin_user` instead."""
    user = User(
        email="test.user@example.com",
        hashed_password=hash_password("TestPassword123!"),
        full_name="Test User",
        role=UserRole.reviewer_l2,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="session")
def sample_document_pages() -> dict[str, list[RenderedPage]]:
    """Every real PDF in sample-documents/, pre-rendered once (session
    scope — rendering is the expensive part, and multiple test files
    reuse this same set) — used by tests/test_ela_service.py and
    tests/test_copy_move_service.py to confirm neither check
    false-positives (or hangs) on this project's actual sample
    documents, not just synthetic fixtures."""
    if not _SAMPLE_DOCUMENTS_DIR.is_dir():
        pytest.skip(f"sample-documents/ not found at {_SAMPLE_DOCUMENTS_DIR}")
    pages_by_name = {}
    for path in sorted(_SAMPLE_DOCUMENTS_DIR.glob("*.pdf")):
        pages_by_name[path.name] = render_pdf_pages(path.read_bytes())
    if not pages_by_name:
        pytest.skip(f"no PDFs found in {_SAMPLE_DOCUMENTS_DIR}")
    return pages_by_name


@pytest.fixture()
def auth_headers(seeded_user):
    return _headers_for(seeded_user)


def _make_user(db_session, email, role, full_name=None, company_id=None):
    """A user of the default test company (or of `company_id`). Pass
    role=UserRole.platform_admin for a platform account (no company)."""
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        user = User(
            email=email,
            hashed_password=hash_password("TestPassword123!"),
            full_name=full_name,
            role=role,
            is_active=True,
            company_id=None
            if role == UserRole.platform_admin
            else (company_id or db_session.info["test_company_id"]),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user
    finally:
        tenancy.restore(db_session, state)


make_user = _make_user


def _headers_for(user):
    token = create_access_token(subject=str(user.id), extra_claims=token_claims(user))
    return {"Authorization": f"Bearer {token}"}


headers_for = _headers_for


@pytest.fixture()
def platform_admin_user(db_session):
    return _make_user(db_session, "platform.admin@example.com", UserRole.platform_admin, "Pat Platform")


@pytest.fixture()
def platform_admin_headers(platform_admin_user):
    return _headers_for(platform_admin_user)


@pytest.fixture()
def reviewer_user(db_session):
    return _make_user(db_session, "reviewer@example.com", UserRole.reviewer_l1, "Rita Reviewer")


@pytest.fixture()
def reviewer_headers(reviewer_user):
    return _headers_for(reviewer_user)


@pytest.fixture()
def plain_user(db_session):
    return _make_user(db_session, "submitter@example.com", UserRole.user, "Sam Submitter")


@pytest.fixture()
def plain_headers(plain_user):
    return _headers_for(plain_user)


@pytest.fixture()
def other_plain_user(db_session):
    return _make_user(db_session, "other.submitter@example.com", UserRole.user, "Olive Other")


@pytest.fixture()
def other_plain_headers(other_plain_user):
    return _headers_for(other_plain_user)


@pytest.fixture()
def l2_reviewer_user(db_session):
    return _make_user(db_session, "l2.reviewer@example.com", UserRole.reviewer_l2, "Lena Level-Two")


@pytest.fixture()
def l2_reviewer_headers(l2_reviewer_user):
    return _headers_for(l2_reviewer_user)
