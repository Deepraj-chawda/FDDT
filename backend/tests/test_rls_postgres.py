"""
Multi-tenancy, database layer: PostgreSQL Row-Level Security on its own.

Every query here is raw SQL with NO company_id filter — the application-layer
protection is deliberately absent — run as the app's ordinary database role
(`fddt_app`, NOSUPERUSER NOBYPASSRLS). The database alone must keep the
companies apart.

Needs a real, migrated PostgreSQL. Point it at the OWNER connection of such a
database (rows are created/removed through it):

    FDDT_RLS_TEST_DATABASE_URL=postgresql+psycopg2://docauth:docauth@localhost:5432/docauth \
        pytest tests/test_rls_postgres.py

Skipped when the variable is not set (the default SQLite suite can't run it).
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.core.config import settings
from app.db import tenancy

OWNER_URL = os.environ.get("FDDT_RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not OWNER_URL, reason="FDDT_RLS_TEST_DATABASE_URL not set (needs a migrated Postgres)")

TENANT_TABLES = (
    "cases", "documents", "document_checks", "document_page_hashes", "cross_document_findings",
    "case_actions", "case_risk_assessments", "risk_scores", "risk_rules", "risk_settings",
    "issuer_registry", "signature_references", "signature_matches", "case_reports", "audit_log",
    "users", "company_usage_stats", "bulk_uploads", "bulk_upload_cases",
)


def test_the_list_covers_every_tenant_owned_model():
    """A new TenantScopedMixin model must be added above, so that its RLS
    policies are actually verified."""
    from app.models import Base
    from app.models.company import TenantScopedMixin

    tenant_tables = {
        mapper.class_.__tablename__ for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScopedMixin)
    }
    assert tenant_tables <= set(TENANT_TABLES), tenant_tables - set(TENANT_TABLES)


def _role_url(user: str, password: str):
    return make_url(OWNER_URL).set(username=user, password=password)


@pytest.fixture(scope="module")
def engines():
    owner = create_engine(OWNER_URL, future=True)
    app = create_engine(_role_url(settings.database_app_user, settings.database_app_password), future=True)
    platform = create_engine(
        _role_url(settings.database_platform_user, settings.database_platform_password), future=True
    )
    yield owner, app, platform
    for engine in (owner, app, platform):
        engine.dispose()


@pytest.fixture(scope="module")
def two_companies(engines):
    """Company A and Company B, each with a user, a case, a document, an
    issuer and an audit row — created through the owner connection."""
    owner, _, _ = engines
    tag = uuid.uuid4().hex[:8]
    ids = {}
    with owner.begin() as conn:
        for key in ("a", "b"):
            company_id, user_id, case_id, doc_id, issuer_id = (uuid.uuid4() for _ in range(5))
            conn.execute(text("INSERT INTO companies (id, name, is_active, max_file_size_mb, max_zip_size_mb) VALUES (:id, :name, true, 10, 300)"),
                         {"id": company_id, "name": f"RLS test {key.upper()} {tag}"})
            conn.execute(text(
                "INSERT INTO users (id, company_id, email, hashed_password, role, is_active, created_at, updated_at) "
                "VALUES (:id, :c, :email, 'x', 'reviewer_l2', true, now(), now())"
            ), {"id": user_id, "c": company_id, "email": f"rls.{key}.{tag}@example.com"})
            conn.execute(text(
                "INSERT INTO cases (id, company_id, case_number, case_type, submitted_by_user_id, status, "
                "assigned_tier, created_at, updated_at) VALUES (:id, :c, :num, 'other', :u, 'submitted', 'l1', now(), now())"
            ), {"id": case_id, "c": company_id, "num": f"RLS-{key.upper()}-{tag}", "u": user_id})
            conn.execute(text(
                "INSERT INTO documents (id, company_id, case_id, uploaded_by_user_id, original_filename, "
                "blob_storage_path, file_hash, file_size_bytes, processing_status, created_at, updated_at) "
                "VALUES (:id, :c, :case, :u, :fn, 'x', :h, 10, 'pending', now(), now())"
            ), {"id": doc_id, "c": company_id, "case": case_id, "u": user_id, "fn": f"{key}.pdf", "h": tag})
            conn.execute(text(
                "INSERT INTO issuer_registry (id, company_id, name, type, is_active, created_at, updated_at) "
                "VALUES (:id, :c, :n, 'vendor', true, now(), now())"
            ), {"id": issuer_id, "c": company_id, "n": f"Vendor {key} {tag}"})
            conn.execute(text(
                "INSERT INTO audit_log (id, company_id, case_id, event_type, created_at) "
                "VALUES (:id, :c, :case, 'rls_test', now())"
            ), {"id": uuid.uuid4(), "c": company_id, "case": case_id})
            ids[key] = {"company": company_id, "user": user_id, "case": case_id, "document": doc_id, "issuer": issuer_id}
    yield ids
    with owner.begin() as conn:
        company_ids = [ids["a"]["company"], ids["b"]["company"]]
        for table in ("audit_log", "issuer_registry", "documents", "cases", "users"):
            conn.execute(text(f"DELETE FROM {table} WHERE company_id = ANY(:ids)"), {"ids": company_ids})
        conn.execute(text("DELETE FROM companies WHERE id = ANY(:ids)"), {"ids": company_ids})


def _as_company(conn, company_id):
    conn.execute(text("SELECT set_config('app.current_company_id', :c, true)"), {"c": str(company_id)})


def test_app_role_is_not_privileged(engines):
    _, app, _ = engines
    with app.connect() as conn:
        row = conn.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )).one()
    assert row == (False, False)


def test_rls_is_enabled_on_every_tenant_table(engines):
    owner, _, _ = engines
    with owner.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:t) AND relkind = 'r'"
        ), {"t": list(TENANT_TABLES) + ["companies"]}).all())
    assert set(rows) == set(TENANT_TABLES) | {"companies"}
    assert all(rows.values()), {t: on for t, on in rows.items() if not on}


def test_unfiltered_select_returns_only_the_current_companys_rows(engines, two_companies):
    _, app, _ = engines
    a, b = two_companies["a"], two_companies["b"]
    with app.begin() as conn:
        _as_company(conn, a["company"])
        # No WHERE company_id = ... anywhere below.
        case_companies = set(conn.execute(text("SELECT company_id FROM cases")).scalars())
        doc_ids = set(conn.execute(text("SELECT id FROM documents")).scalars())
        issuers = set(conn.execute(text("SELECT id FROM issuer_registry")).scalars())
        audit_companies = set(conn.execute(text("SELECT company_id FROM audit_log")).scalars())
        users = set(conn.execute(text("SELECT id FROM users")).scalars())
        companies = set(conn.execute(text("SELECT id FROM companies")).scalars())
    assert case_companies == {a["company"]}
    assert a["document"] in doc_ids and b["document"] not in doc_ids
    assert a["issuer"] in issuers and b["issuer"] not in issuers
    assert audit_companies == {a["company"]}
    assert a["user"] in users and b["user"] not in users
    assert companies == {a["company"]}


def test_looking_up_another_companys_row_by_id_finds_nothing(engines, two_companies):
    _, app, _ = engines
    a, b = two_companies["a"], two_companies["b"]
    with app.begin() as conn:
        _as_company(conn, a["company"])
        assert conn.execute(text("SELECT * FROM cases WHERE id = :id"), {"id": b["case"]}).first() is None
        assert conn.execute(text("SELECT * FROM documents WHERE case_id = :id"), {"id": b["case"]}).first() is None


def test_no_company_context_means_no_rows(engines, two_companies):
    _, app, _ = engines
    with app.begin() as conn:
        for table in ("cases", "documents", "issuer_registry", "risk_rules", "audit_log", "users", "companies"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table


def test_cannot_update_or_delete_another_companys_rows(engines, two_companies):
    _, app, owner_check = engines[1], engines[1], engines[0]
    a, b = two_companies["a"], two_companies["b"]
    with app.begin() as conn:
        _as_company(conn, a["company"])
        updated = conn.execute(text("UPDATE cases SET status = 'rejected' WHERE id = :id"), {"id": b["case"]})
        assert updated.rowcount == 0
        deleted = conn.execute(text("DELETE FROM cross_document_findings WHERE case_id = :id"), {"id": b["case"]})
        assert deleted.rowcount == 0
    with owner_check.connect() as conn:
        assert conn.execute(text("SELECT status FROM cases WHERE id = :id"), {"id": b["case"]}).scalar_one() == "submitted"


def test_moving_a_row_to_another_company_is_refused(engines, two_companies):
    _, app, _ = engines
    a, b = two_companies["a"], two_companies["b"]
    with pytest.raises(DBAPIError, match="row-level security"):
        with app.begin() as conn:
            _as_company(conn, a["company"])
            conn.execute(text("UPDATE issuer_registry SET company_id = :b WHERE id = :id"),
                         {"b": b["company"], "id": a["issuer"]})


def test_inserting_a_row_for_another_company_is_refused(engines, two_companies):
    _, app, _ = engines
    a, b = two_companies["a"], two_companies["b"]
    with pytest.raises(DBAPIError, match="row-level security"):
        with app.begin() as conn:
            _as_company(conn, a["company"])
            conn.execute(text(
                "INSERT INTO issuer_registry (id, company_id, name, type, is_active, created_at, updated_at) "
                "VALUES (:id, :b, 'smuggled', 'vendor', true, now(), now())"
            ), {"id": uuid.uuid4(), "b": b["company"]})


def test_append_only_and_versioned_tables_cannot_be_rewritten(engines, two_companies):
    _, app, platform = engines
    a = two_companies["a"]
    for engine in (app, platform):
        for statement in (
            "UPDATE audit_log SET event_type = 'tampered'",
            "DELETE FROM audit_log",
            "UPDATE risk_rules SET weight = 0",
            "DELETE FROM case_risk_assessments",
        ):
            with pytest.raises(ProgrammingError, match="permission denied"):
                with engine.begin() as conn:
                    _as_company(conn, a["company"])
                    conn.execute(text(statement))


def test_no_role_has_bypassrls(engines):
    """Platform access is policy-based (platform_all), never BYPASSRLS — so it
    works on managed PostgreSQL where BYPASSRLS can't be granted."""
    owner, _, _ = engines
    with owner.connect() as conn:
        rows = dict(conn.execute(text(
            "SELECT rolname, rolbypassrls OR rolsuper FROM pg_roles WHERE rolname IN (:a, :p)"
        ), {"a": settings.database_app_user, "p": settings.database_platform_user}).all())
    assert rows == {settings.database_app_user: False, settings.database_platform_user: False}


def test_every_rls_table_has_the_platform_policy(engines):
    owner, _, _ = engines
    with owner.connect() as conn:
        covered = set(conn.execute(text(
            "SELECT tablename FROM pg_policies WHERE policyname = 'platform_all' AND :role = ANY(roles)"
        ), {"role": settings.database_platform_user}).scalars())
    assert covered == set(TENANT_TABLES) | {"companies"}


def test_platform_role_sees_every_company_through_its_policy(engines, two_companies):
    _, _, platform = engines
    a, b = two_companies["a"], two_companies["b"]
    with platform.connect() as conn:
        seen = set(conn.execute(text("SELECT id FROM cases WHERE id IN (:a, :b)"), {"a": a["case"], "b": b["case"]}).scalars())
        # ...even with a company context set, the platform policy still applies.
        conn.execute(text("SELECT set_config('app.current_company_id', :c, false)"), {"c": str(a["company"])})
        still = set(conn.execute(text("SELECT id FROM cases WHERE id IN (:a, :b)"), {"a": a["case"], "b": b["case"]}).scalars())
    assert seen == still == {a["case"], b["case"]}


def test_platform_role_can_write_any_company(engines, two_companies):
    _, _, platform = engines
    b = two_companies["b"]
    with platform.begin() as conn:
        updated = conn.execute(text("UPDATE issuer_registry SET is_active = true WHERE id = :id"), {"id": b["issuer"]})
    assert updated.rowcount == 1


def test_platform_admin_access_rows_are_invisible_to_the_app_role(engines, two_companies):
    """Platform-only audit rows (company_id NULL) — even an app-role query
    with no event-type filter can't see them."""
    owner, app, _ = engines
    a = two_companies["a"]
    marker = uuid.uuid4()
    with owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO audit_log (id, company_id, case_id, event_type, event_data, created_at) "
            "VALUES (:id, NULL, :case, 'platform_admin_access', CAST(:data AS jsonb), now())"
        ), {"id": marker, "case": a["case"], "data": f'{{"company_id": "{a["company"]}"}}'})
    try:
        with app.begin() as conn:
            _as_company(conn, a["company"])
            rows = conn.execute(text("SELECT id FROM audit_log WHERE case_id = :case"), {"case": a["case"]}).scalars().all()
        assert marker not in rows
    finally:
        with owner.begin() as conn:
            conn.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": marker})


def test_every_upsert_the_pipeline_uses_works_under_the_app_role(engines, two_companies):
    """The idempotent writes (page hashes, check rows, usage counters) run as
    `fddt_app`, whose grants are least-privilege. Each is executed twice, so
    both the insert and the ON CONFLICT path are exercised; a conflict clause
    that needs a privilege the role lacks (e.g. DO UPDATE on an insert-only
    table) fails here with "permission denied". Rolled back afterwards."""
    from sqlalchemy.orm import sessionmaker

    from app.models.document import Document
    from app.models.document_check import DocumentCheckStatus, DocumentCheckType
    from app.services.check_store import save_check
    from app.services.forensics.duplicate_check import store_page_hashes
    from app.services.usage_service import record_document_uploaded

    _, app, _ = engines
    a = two_companies["a"]
    session = tenancy.bind_company(sessionmaker(bind=app, future=True)(), a["company"])
    try:
        document = session.get(Document, a["document"])
        for _ in range(2):
            store_page_hashes(session, company_id=a["company"], document_id=a["document"],
                              page_hashes=["0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0"])
            save_check(session, document=document, check_type=DocumentCheckType.duplicate_detection,
                       status=DocumentCheckStatus.completed, result={"result": "pass", "details": []})
            record_document_uploaded(session, a["company"], 10)
            session.flush()
        hashes = session.execute(text(
            "SELECT count(*) FROM document_page_hashes WHERE document_id = :d"), {"d": a["document"]}).scalar_one()
        assert hashes == 2
    finally:
        session.rollback()
        session.close()


def test_the_apps_own_session_machinery_sets_the_company_for_rls(engines, two_companies):
    """Same check through the real RoutingSession + tenancy hook, using a
    text() query (no ORM entity, so the application-layer filter cannot
    apply) — only RLS stands between the session and company B's rows."""
    from sqlalchemy.orm import sessionmaker

    _, app, _ = engines
    a, b = two_companies["a"], two_companies["b"]
    Session = sessionmaker(bind=app, future=True)
    session = tenancy.bind_company(Session(), a["company"])
    try:
        rows = set(session.execute(text("SELECT company_id FROM cases")).scalars())
        assert rows == {a["company"]}
        session.commit()  # a new transaction re-applies the company setting
        rows = set(session.execute(text("SELECT id FROM documents")).scalars())
        assert b["document"] not in rows and a["document"] in rows
    finally:
        session.close()
